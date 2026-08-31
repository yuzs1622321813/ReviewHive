"""长期记忆（单用户）：preference（偏好）/ project_fact（项目事实）/ lesson（经验教训）。

设计参考 NousResearch/hermes-agent 的 agent-curated memory 与摘要召回思想，实现完全本地化：
- SQLite 存元数据（类型、热度、状态），Qdrant 独立集合存向量（复用 bge-m3）；
- 生命周期三机制：过期（lesson TTL 归档 + 归档清理）、压缩（相似簇合并）、
  摘要（LLM 蒸馏最老 lesson）；
- 读写收口：评审前 recall() 注入，评审后 reflect() 提取写回。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from reviewhive.config import MemoryConfig
from reviewhive.models.llm import LLMClient

logger = logging.getLogger(__name__)

MemoryType = Literal["preference", "project_fact", "lesson"]
MEMORY_TYPES = ("preference", "project_fact", "lesson")

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_DAY = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    source_session TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    archived_at REAL
);
"""

_REFLECT_PROMPT = """你是 ReviewHive 代码评审系统的反思模块。请从本次评审中提炼值得长期记住的内容，供未来评审复用。

可记忆三类：
- preference：用户的编码偏好或对评审尺度的要求（如"不要报 info 级命名问题"）
- project_fact：项目的稳定事实（如"使用 MyBatis-Plus"、"Java 17"）
- lesson：评审经验教训（如"该用户常忽略资源关闭问题，需明确指出后果"）

只输出 JSON：{"memories": [{"type": "preference|project_fact|lesson", "content": "一句话"}]}
要求：最多 3 条；只提炼可复用的规则，不记录本次的具体代码细节；没有值得记的就输出空数组。"""


class Memory(BaseModel):
    id: str = ""
    type: str = "lesson"
    content: str = ""
    importance: float = 0.5
    source_session: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    status: str = "active"
    archived_at: float | None = None


class MemoryStore:
    """SQLite 元数据存储。"""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _row_to_memory(self, row) -> Memory:
        return Memory(
            id=row[0], type=row[1], content=row[2], importance=row[3], source_session=row[4],
            created_at=row[5], last_used_at=row[6], use_count=row[7], status=row[8], archived_at=row[9],
        )

    def insert(self, memory: Memory) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                (memory.id, memory.type, memory.content, memory.importance, memory.source_session,
                 memory.created_at, memory.last_used_at, memory.use_count, memory.status, memory.archived_at),
            )
            self._conn.commit()

    def delete(self, memory_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()

    def touch(self, memory_id: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()

    def set_status(self, memory_id: str, status: str, archived_at: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET status = ?, archived_at = ? WHERE id = ?",
                (status, archived_at, memory_id),
            )
            self._conn.commit()

    def list_by_status(self, status: str, type_: str | None = None) -> list[Memory]:
        query = "SELECT * FROM memories WHERE status = ?"
        params: list = [status]
        if type_:
            query += " AND type = ?"
            params.append(type_)
        query += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MemoryBank:
    """记忆门面：存储 + 向量召回 + 反思写入 + 生命周期维护。"""

    def __init__(self, cfg: MemoryConfig, store: MemoryStore, embedder, qdrant, llm: LLMClient):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.qdrant = qdrant
        self.llm = llm
        self._session_count = 0

    # ---------- 基础读写 ----------

    def ensure(self) -> None:
        dim = self.embedder.dim or len(self.embedder.encode(["dim"])[0])
        if not self.qdrant.collection_exists(self.cfg.collection):
            self.qdrant.create_memory_collection(self.cfg.collection, dim)

    def add(self, content: str, type_: str, source_session: str = "", importance: float = 0.5) -> Memory | None:
        now = time.time()
        cleaned = content.strip()[:500]
        vector = self.embedder.encode([cleaned])[0]

        hits = self.qdrant.search_memory(self.cfg.collection, vector, top_k=1)
        if hits:
            _, payload = hits[0]
            existing_content = payload.get("content", "")
            if existing_content:
                existing_vec = self.embedder.encode([existing_content])[0]
                if _cosine(vector, existing_vec) > self.cfg.dedup_similarity:
                    logger.info("记忆去重跳过 [cos>%.2f]: %s", self.cfg.dedup_similarity, cleaned[:60])
                    return None

        memory = Memory(
            id=uuid.uuid4().hex[:12],
            type=type_ if type_ in MEMORY_TYPES else "lesson",
            content=cleaned,
            importance=importance,
            source_session=source_session,
            created_at=now,
            last_used_at=now,
        )
        self.qdrant.upsert_memory(self.cfg.collection, memory.id, vector, memory.model_dump())
        self.store.insert(memory)
        logger.info("记忆入库 [%s] %s", memory.type, memory.content[:60])
        return memory

    def remove(self, memory_id: str) -> None:
        self.store.delete(memory_id)
        self.qdrant.delete_memory(self.cfg.collection, memory_id)

    def list_active(self) -> list[Memory]:
        return self.store.list_by_status("active")

    def recall(self, query: str, top_k: int | None = None) -> list[Memory]:
        """向量召回活跃记忆，并登记使用热度。"""
        hits = self.qdrant.search_memory(self.cfg.collection, self.embedder.encode([query])[0], top_k or self.cfg.recall_top_k)
        now = time.time()
        memories: list[Memory] = []
        for memory_id, payload in hits:
            try:
                memory = Memory(**payload)
            except Exception:
                continue
            self.store.touch(memory.id, now)
            memories.append(memory)
        return memories

    @staticmethod
    def render_notes(memories: list[Memory]) -> str:
        if not memories:
            return ""
        label = {"preference": "偏好", "project_fact": "项目事实", "lesson": "经验"}
        lines = [f"- [{label.get(m.type, m.type)}] {m.content}" for m in memories]
        return "以下是你记得的关于该用户的长期记忆，评审时请遵循其中的偏好：\n" + "\n".join(lines)

    # ---------- 反思写入 ----------

    async def reflect(self, session_id: str, summary: str, finding_titles: list[str], code_head: str) -> list[Memory]:
        """评审结束后由主模型提炼候选记忆（失败静默降级）。"""
        user_content = (
            f"本次评审总结：{summary[:600]}\n"
            f"发现项：{'; '.join(finding_titles[:10])}\n"
            f"被评审代码（片段）：\n{code_head[:1200]}"
        )
        try:
            data = await self.llm.chat_json(
                [
                    {"role": "system", "content": _REFLECT_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning("记忆反思失败: %s", exc)
            return []
        added: list[Memory] = []
        for item in (data.get("memories") or [])[:3]:
            content = str(item.get("content", "")).strip()
            if len(content) < 8:
                continue
            memory = self.add(content, str(item.get("type", "lesson")), source_session=session_id)
            if memory is not None:
                added.append(memory)
        return added

    # ---------- 生命周期三机制 ----------

    def note_session(self) -> bool:
        """登记一次评审会话，返回是否到达维护时机。"""
        self._session_count += 1
        return self._session_count % max(self.cfg.maintain_every_sessions, 1) == 0

    def maintain(self, now: float | None = None) -> dict[str, int]:
        now = now or time.time()
        return {
            "expired": self._expire(now),
            "merged": self._compress(),
            "summarized": 0,  # 摘要为异步，由 summarize_async 单独统计
        }

    def _expire(self, now: float) -> int:
        ttl = self.cfg.lesson_ttl_days * _DAY
        retention = self.cfg.archive_retention_days * _DAY
        expired = 0
        for memory in self.store.list_by_status("active", "lesson"):
            if now - max(memory.last_used_at, memory.created_at) > ttl:
                self.store.set_status(memory.id, "archived", now)
                self.qdrant.delete_memory(self.cfg.collection, memory.id)
                expired += 1
        for memory in self.store.list_by_status("archived"):
            if memory.archived_at and now - memory.archived_at > retention:
                self.store.delete(memory.id)
        return expired

    def _compress(self) -> int:
        active = self.store.list_by_status("active")
        if len(active) <= self.cfg.compress_threshold:
            return 0
        vectors = self.embedder.encode([memory.content for memory in active])
        merged_count = 0
        removed: set[int] = set()
        for i in range(len(active)):
            if i in removed:
                continue
            cluster = [i]
            for j in range(i + 1, len(active)):
                if j in removed:
                    continue
                if _cosine(vectors[i], vectors[j]) > self.cfg.compress_similarity:
                    cluster.append(j)
                    removed.add(j)
            if len(cluster) < 2:
                continue
            members = [active[idx] for idx in cluster]
            keep = max(members, key=lambda m: (m.use_count, m.last_used_at))
            keep.use_count = sum(m.use_count for m in members)
            keep.importance = max(m.importance for m in members)
            self.store.insert(keep)
            for other in members:
                if other.id != keep.id:
                    self.remove(other.id)
            merged_count += len(cluster) - 1
        if merged_count:
            logger.info("记忆压缩：合并 %d 条相似记忆", merged_count)
        return merged_count

    async def summarize_async(self) -> int:
        """LLM 蒸馏：把最老的一批 lesson 摘要为更少的通用经验。"""
        lessons = self.store.list_by_status("active", "lesson")
        if len(lessons) <= self.cfg.summarize_threshold:
            return 0
        batch = lessons[: self.cfg.summarize_batch]
        content_lines = "\n".join(f"- {m.content}" for m in batch)
        try:
            data = await self.llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是记忆压缩模块。把下列零散评审经验蒸馏成不超过 3 条更通用、可复用的经验。"
                            "只输出 JSON：{\"summaries\": [\"...\", ...]}"
                        ),
                    },
                    {"role": "user", "content": content_lines},
                ],
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning("记忆摘要失败: %s", exc)
            return 0
        summaries = [str(s).strip() for s in (data.get("summaries") or []) if str(s).strip()][:3]
        if not summaries:
            return 0
        for memory in batch:
            self.remove(memory.id)
        for text in summaries:
            self.add(text, "lesson", source_session="summarized", importance=0.7)
        logger.info("记忆摘要：%d 条 lesson 蒸馏为 %d 条", len(batch), len(summaries))
        return len(batch)
