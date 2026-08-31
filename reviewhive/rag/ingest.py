"""知识库入库：种子语料（JSON）+ 开源数据集（JSONL）+ 文档（md/pdf）-> 统一 KBChunk -> 双库写入。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from reviewhive.config import Settings
from reviewhive.core.schema import KBChunk
from reviewhive.models.embedding import BaseEmbedder
from reviewhive.rag.chunking import Chunker, build_chunker
from reviewhive.rag.documents import load_documents
from reviewhive.rag.keywordstore import ESStore
from reviewhive.rag.vectorstore import QdrantStore

logger = logging.getLogger(__name__)


def load_seed_corpus(corpus_dir: Path) -> list[KBChunk]:
    chunks: list[KBChunk] = []
    if not corpus_dir.exists():
        return chunks
    for path in sorted(corpus_dir.glob("*.json")):
        records = json.loads(path.read_text(encoding="utf-8"))
        for record in records:
            record.setdefault("source", path.stem)
            chunks.append(KBChunk(**record).ensure_id())
    return chunks


def load_downloaded(downloads_dir: Path, chunker: Chunker) -> list[KBChunk]:
    chunks: list[KBChunk] = []
    if not downloads_dir.exists():
        return chunks
    for path in sorted(downloads_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            chunk = KBChunk(**record).ensure_id()
            if len(chunk.content) > chunker.max_chars:
                for i, piece in enumerate(chunker.window.split(chunk.content)):
                    sub = chunk.model_copy(update={"content": piece, "id": f"{chunk.id}-{i}"})
                    chunks.append(sub)
            else:
                chunks.append(chunk)
    return chunks


def ingest(
    settings: Settings,
    embedder: BaseEmbedder,
    vectorstore: QdrantStore,
    keywordstore: ESStore,
    batch_size: int = 32,
) -> dict[str, int]:
    data_dir = Path(settings.app.data_dir)
    chunker = build_chunker(settings.rag.chunker)
    chunks = (
        load_seed_corpus(data_dir / "corpus")
        + load_downloaded(data_dir / "downloads", chunker)
        + load_documents(data_dir / "docs", chunker)
    )
    if not chunks:
        logger.warning("没有可入库的语料：先准备 data/corpus、data/docs 或运行 reviewhive download-data")
        return {"total": 0}

    dim = embedder.dim or len(embedder.encode([chunks[0].display_text()])[0])
    vectorstore.ensure(dim)
    keywordstore.ensure()

    seen: set[str] = set()
    unique: list[KBChunk] = []
    for chunk in chunks:
        if chunk.id in seen:
            continue
        seen.add(chunk.id)
        unique.append(chunk)

    written = 0
    for start in range(0, len(unique), batch_size):
        batch = unique[start : start + batch_size]
        vectors = embedder.encode([chunk.display_text() for chunk in batch])
        vectorstore.upsert(batch, vectors)
        keywordstore.index_chunks(batch)
        written += len(batch)
        logger.info("已入库 %d/%d", written, len(unique))

    return {"total": len(unique), "qdrant": vectorstore.count(), "elasticsearch": keywordstore.count()}
