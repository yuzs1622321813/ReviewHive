"""评审流水线：受理 -> 规划 -> 检索 -> 并行评审 -> 汇总（混合编排主干）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from reviewhive.agents.base import SubAgent
from reviewhive.agents.orchestrator import Orchestrator
from reviewhive.agents.profiles import ALL_PROFILES
from reviewhive.config import Settings
from reviewhive.core.memory import MemoryBank, MemoryStore
from reviewhive.core.schema import (
    SEVERITIES,
    AgentResult,
    ImageAttachment,
    ReviewInput,
    ReviewReport,
    RetrievedChunk,
    StreamEvent,
)
from reviewhive.core.store import SessionStore
from reviewhive.models.embedding import BaseEmbedder, build_embedder
from reviewhive.models.llm import LLMClient
from reviewhive.models.reranker import CrossEncoderReranker, build_reranker
from reviewhive.models.vision import VisionClient
from reviewhive.observability import INPUT_VALUE, KIND_CHAIN, KIND_RETRIEVER, OUTPUT_VALUE, span
from reviewhive.rag.keywordstore import ESStore
from reviewhive.rag.retriever import HybridRetriever
from reviewhive.rag.vectorstore import QdrantStore
from reviewhive.resilience import CircuitBreaker
from reviewhive.skills.builtin import build_standard_registry, build_vision_registry
from reviewhive.skills.context import ReviewWorkspace, SkillContext

logger = logging.getLogger(__name__)

Emit = Callable[[StreamEvent], Awaitable[None]]

_LANG_EXT = {"java": "MainCode.java", "python": "main.py", "go": "main.go", "javascript": "main.js", "typescript": "main.ts"}


class HiveDeps:
    """进程级依赖容器：客户端即时创建，重模型（嵌入/重排）惰性加载。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        res = settings.resilience
        self.llm = LLMClient(settings.models.llm, resilience=res)
        self.vision = VisionClient(settings.models.vision, resilience=res)
        self.vectorstore = QdrantStore(
            settings.rag.qdrant_url,
            settings.rag.collection,
            breaker=CircuitBreaker(
                name="qdrant",
                failure_threshold=res.qdrant_cb.failure_threshold,
                recovery_timeout=res.qdrant_cb.recovery_timeout,
                success_threshold=res.qdrant_cb.success_threshold,
            ),
        )
        self.keywordstore = ESStore(
            settings.rag.es_url,
            settings.rag.es_index,
            breaker=CircuitBreaker(
                name="es",
                failure_threshold=res.es_cb.failure_threshold,
                recovery_timeout=res.es_cb.recovery_timeout,
                success_threshold=res.es_cb.success_threshold,
            ),
        )
        self.standard_registry = build_standard_registry()
        self.vision_registry = build_vision_registry()
        self.orchestrator = Orchestrator(
            self.llm,
            configured_agents=settings.review.sub_agents,
            vision_enabled=settings.models.vision.enabled,
        )
        self._embedder: BaseEmbedder | None = None
        self._reranker: CrossEncoderReranker | None = None
        self._retriever: HybridRetriever | None = None
        self._memory_bank: MemoryBank | None = None
        self._load_lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        return self._load_lock

    async def embedder(self) -> BaseEmbedder:
        if self._embedder is None:
            async with self._lock():
                if self._embedder is None:
                    self._embedder = await asyncio.to_thread(build_embedder, self.settings.models.embedding)
        return self._embedder

    async def retriever(self) -> HybridRetriever:
        if self._retriever is None:
            async with self._lock():
                if self._retriever is None:
                    if self._embedder is None:
                        self._embedder = await asyncio.to_thread(build_embedder, self.settings.models.embedding)
                    reranker = None
                    if self.settings.models.reranker.enabled:
                        try:
                            reranker = await asyncio.to_thread(build_reranker, self.settings.models.reranker)
                        except Exception as exc:
                            logger.warning("重排模型加载失败，退化为无重排: %s", exc)
                    self._retriever = HybridRetriever(
                        self.settings.rag, self.vectorstore, self.keywordstore, self._embedder, reranker
                    )
        return self._retriever

    async def memory_bank(self) -> MemoryBank | None:
        if not self.settings.memory.enabled:
            return None
        if self._memory_bank is None:
            async with self._lock():
                if self._memory_bank is None:
                    if self._embedder is None:
                        self._embedder = await asyncio.to_thread(build_embedder, self.settings.models.embedding)
                    mem_store = MemoryStore(self.settings.app.db_path)
                    bank = MemoryBank(self.settings.memory, mem_store, self._embedder, self.vectorstore, self.llm)
                    await asyncio.to_thread(bank.ensure)
                    self._memory_bank = bank
        return self._memory_bank

    def build_agent(self, name: str) -> SubAgent:
        profile = ALL_PROFILES[name]
        registry = self.vision_registry if name == "vision" else self.standard_registry
        return SubAgent(profile, self.llm, registry, max_loops=self.settings.review.max_skill_loops)

    async def close(self) -> None:
        await self.llm.close()
        await self.vision.close()

    async def health_check(self) -> dict[str, bool]:
        """返回各服务健康状态。"""
        import httpx

        results: dict[str, bool] = {}
        results["llm"] = await self.llm.healthy()
        if self.settings.models.vision.enabled:
            results["vision"] = await self.vision.healthy()
        timeout = self.settings.resilience.health_check_timeout
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.settings.rag.qdrant_url}/collections")
                results["qdrant"] = resp.status_code == 200
        except Exception:
            results["qdrant"] = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.settings.rag.es_url}/_cluster/health")
                results["es"] = resp.status_code == 200
        except Exception:
            results["es"] = False
        return results


class ReviewPipeline:
    def __init__(self, deps: HiveDeps, store: SessionStore):
        self.deps = deps
        self.store = store

    async def run(self, review_input: ReviewInput, emit: Emit, session_id: str | None = None) -> ReviewReport:
        session_id = session_id or uuid.uuid4().hex[:12]
        report = ReviewReport(session_id=session_id)
        started = time.monotonic()

        # 降级检查：LLM 不可用则直接拒绝
        if not self.deps.llm.is_available:
            report.status = "failed"
            report.summary = "LLM 服务不可用（熔断器打开），无法评审"
            report.duration_ms = int((time.monotonic() - started) * 1000)
            self.store.finish(session_id, report.status, report.model_dump_json())
            await emit(StreamEvent(type="error", data={"message": report.summary}))
            await emit(StreamEvent(type="report", data=json.loads(report.model_dump_json())))
            return report

        # 降级状态汇总
        services_status: dict[str, str] = {"llm": "ok"}
        skipped_agents: list[str] = []
        degradation_messages: list[str] = []

        if self.deps.settings.models.vision.enabled:
            if not self.deps.vision.is_available:
                services_status["vision"] = "degraded"
                skipped_agents.append("vision")
                degradation_messages.append("Vision 服务不可用，已跳过多模态评审")
            else:
                services_status["vision"] = "ok"

        # 检索器状态（Qdrant/ES 熔断器状态）
        if self.deps.vectorstore._breaker and self.deps.vectorstore._breaker.is_open:
            services_status["qdrant"] = "degraded"
            degradation_messages.append("Qdrant 不可用，仅使用关键词检索")
        else:
            services_status["qdrant"] = "ok"
        if self.deps.keywordstore._breaker and self.deps.keywordstore._breaker.is_open:
            services_status["es"] = "degraded"
            degradation_messages.append("Elasticsearch 不可用，仅使用向量检索")
        else:
            services_status["es"] = "ok"

        if degradation_messages:
            await emit(StreamEvent(type="degradation", data={
                "services": services_status,
                "skipped_agents": skipped_agents,
                "message": "；".join(degradation_messages),
            }))

        with span(
            "review.session",
            KIND_CHAIN,
            {"session.id": session_id, "input.chars": len(review_input.primary_text())},
        ):
            try:
                workspace = self._intake(review_input, report)
                await emit(StreamEvent(type="phase", data={"phase": "intake", "files": workspace.names()}))

                plan_input = json.dumps({"files": workspace.names(), "has_images": bool(review_input.images)}, ensure_ascii=False)
                with span("phase.plan", KIND_CHAIN, {INPUT_VALUE: plan_input}) as plan_span:
                    report.plan = await self.deps.orchestrator.plan(workspace, review_input.images)
                    if plan_span:
                        plan_summary = json.dumps({"language": report.plan.language, "agents": report.plan.sub_agents, "focus": report.plan.focus_points[:3]}, ensure_ascii=False)
                        plan_span.set_attribute(OUTPUT_VALUE, plan_summary)
                await emit(StreamEvent(type="plan", data=report.plan.model_dump()))

                memory_notes = ""
                bank = await self.deps.memory_bank()
                if bank:
                    query = f"{report.plan.language} {' '.join(report.plan.focus_points[:3])}"
                    memories = await asyncio.to_thread(bank.recall, query)
                    memory_notes = MemoryBank.render_notes(memories)
                    if memories:
                        await emit(StreamEvent(type="phase", data={
                            "phase": "memory",
                            "recalled": [{"type": m.type, "content": m.content} for m in memories],
                        }))

                with span("phase.retrieve", KIND_RETRIEVER) as ret_span:
                    context_chunks = await self._retrieve(report.plan, workspace, report)
                    if ret_span:
                        ret_span.set_attribute(OUTPUT_VALUE, f"chunks={len(context_chunks)}, refs={len(report.context_refs)}")
                await emit(
                    StreamEvent(
                        type="phase",
                        data={"phase": "retrieve", "refs": report.context_refs},
                    )
                )

                agents_csv = ",".join(report.plan.sub_agents)
                with span("phase.review", KIND_CHAIN, {"sub_agents": agents_csv, INPUT_VALUE: agents_csv}) as review_span:
                    results = await self._review(report.plan, workspace, review_input.images, context_chunks, emit, memory_notes)
                    if review_span:
                        total_findings = sum(len(r.findings) for r in results)
                        review_span.set_attribute(OUTPUT_VALUE, f"agents={len(results)}, findings={total_findings}")
                await emit(StreamEvent(type="phase", data={"phase": "aggregate"}))

                with span("phase.aggregate", KIND_CHAIN, {INPUT_VALUE: f"results={len(results)}"}) as agg_span:
                    findings, summary = await self.deps.orchestrator.aggregate(results)
                    if agg_span:
                        agg_span.set_attribute(OUTPUT_VALUE, f"findings={len(findings)}")
                report.findings = sorted(findings, key=lambda f: SEVERITIES.index(f.severity))
                report.summary = summary
                report.language = report.plan.language
                report.status = "done"

                if bank and report.status == "done":
                    asyncio.create_task(self._post_reflect(bank, session_id, report, review_input))
            except Exception as exc:
                logger.exception("评审流水线失败")
                report.status = "failed"
                report.summary = f"流水线异常：{exc}"
                await emit(StreamEvent(type="error", data={"message": str(exc)}))
            finally:
                report.duration_ms = int((time.monotonic() - started) * 1000)
                self.store.finish(session_id, report.status, report.model_dump_json())
                await emit(StreamEvent(type="report", data=json.loads(report.model_dump_json())))
        return report

    def _intake(self, review_input: ReviewInput, report: ReviewReport) -> ReviewWorkspace:
        limit = self.deps.settings.review.max_input_chars
        code = review_input.code[:limit]
        diff = review_input.diff[:limit]
        if not code and not diff and not review_input.images:
            raise ValueError("提交内容为空：请提供代码、diff 或图片")

        workspace = ReviewWorkspace(diff=diff)
        if code:
            name = review_input.filename or _LANG_EXT.get(review_input.language.lower(), "MainCode.java")
            workspace.add_file(name, code)
        return workspace

    async def _retrieve(self, plan, workspace: ReviewWorkspace, report: ReviewReport) -> list[RetrievedChunk]:
        try:
            retriever = await self.deps.retriever()
        except Exception as exc:
            logger.warning("检索器不可用（嵌入模型未安装或配置错误）: %s", exc)
            return []
        query_parts = [f"{plan.language} code review"]
        query_parts.extend(plan.focus_points[:3])
        query_parts.extend(workspace.names()[:2])
        query = " ".join(part for part in query_parts if part)
        try:
            chunks = await asyncio.to_thread(retriever.retrieve, query, 6)
        except Exception as exc:
            logger.warning("预检索失败: %s", exc)
            return []
        report.context_refs = [chunk.chunk.id for chunk in chunks]
        return chunks

    async def _review(
        self,
        plan,
        workspace: ReviewWorkspace,
        images: list[ImageAttachment],
        context_chunks: list[RetrievedChunk],
        emit: Emit,
        memory_notes: str = "",
    ) -> list[AgentResult]:
        names = list(dict.fromkeys(plan.sub_agents))
        if "vision" in names and (not images or not self.deps.settings.models.vision.enabled or not self.deps.vision.is_available):
            names.remove("vision")
        if not names:
            names = list(self.deps.settings.review.sub_agents)

        vision_client = self.deps.vision if self.deps.settings.models.vision.enabled else None
        retriever: HybridRetriever | None = None
        try:
            retriever = await self.deps.retriever()
        except Exception:
            pass
        extra = {"images": images}
        if memory_notes:
            extra["memory_notes"] = memory_notes
        ctx = SkillContext(workspace=workspace, retriever=retriever, vision=vision_client, extra=extra)

        tasks = [
            self.deps.build_agent(name).run(ctx, plan.focus_points, context_chunks, emit)
            for name in names
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[AgentResult] = []
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                logger.warning("子 Agent %s 执行失败: %s", name, outcome)
                await emit(StreamEvent(type="agent_done", data={"agent": name, "findings": 0, "error": str(outcome)}))
                results.append(AgentResult(agent=name, notes=f"执行失败：{outcome}"))
            else:
                results.append(outcome)
        return results

    async def _post_reflect(self, bank: MemoryBank, session_id: str, report: ReviewReport, review_input: ReviewInput) -> None:
        try:
            finding_titles = [f.title for f in report.findings]
            code_head = review_input.primary_text()[:1200]
            await bank.reflect(session_id, report.summary or "", finding_titles, code_head)
            if bank.note_session():
                stats = bank.maintain()
                if sum(stats.values()) > 0:
                    logger.info("记忆维护: %s", stats)
                await bank.summarize_async()
        except Exception as exc:
            logger.warning("评审后记忆处理失败: %s", exc)
