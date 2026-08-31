"""混合检索：Qdrant 向量召回 + ES BM25 召回，RRF 融合，bge-reranker 精排。"""
from __future__ import annotations

import logging

from reviewhive.config import RAGConfig
from reviewhive.core.schema import KBChunk, RetrievedChunk
from reviewhive.models.embedding import BaseEmbedder
from reviewhive.models.reranker import CrossEncoderReranker
from reviewhive.observability import INPUT_VALUE, KIND_RETRIEVER, OUTPUT_VALUE, span
from reviewhive.rag.keywordstore import ESStore
from reviewhive.rag.vectorstore import QdrantStore

logger = logging.getLogger(__name__)


class HybridRetriever:
    def __init__(
        self,
        cfg: RAGConfig,
        vectorstore: QdrantStore,
        keywordstore: ESStore,
        embedder: BaseEmbedder,
        reranker: CrossEncoderReranker | None = None,
    ):
        self.cfg = cfg
        self.vectorstore = vectorstore
        self.keywordstore = keywordstore
        self.embedder = embedder
        self.reranker = reranker

    def retrieve(self, query: str, top_n: int = 6, kind: str | None = None) -> list[RetrievedChunk]:
        with span("rag.retrieve", KIND_RETRIEVER, {INPUT_VALUE: query[:500]}) as active:
            results = self._retrieve(query, top_n, kind)
            if active is not None:
                active.set_attribute(OUTPUT_VALUE, ",".join(hit.chunk.id for hit in results)[:500])
            return results

    def _retrieve(self, query: str, top_n: int = 6, kind: str | None = None) -> list[RetrievedChunk]:
        vector_hits = self._safe_vector(query, kind)
        keyword_hits = self._safe_keyword(query, kind)

        chunks: dict[str, KBChunk] = {}
        rrf_scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}
        for source_name, hits in (("vector", vector_hits), ("keyword", keyword_hits)):
            for rank, (chunk_id, _score, chunk) in enumerate(hits, start=1):
                chunks[chunk_id] = chunk
                rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (self.cfg.rrf_k + rank)
                sources.setdefault(chunk_id, []).append(source_name)

        if not chunks:
            return []

        candidates = sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)
        rerank_pool = candidates[:30]

        if self.reranker is not None:
            docs = [chunks[chunk_id].display_text() for chunk_id, _ in rerank_pool]
            ranked = self.reranker.rank(query, docs, top_n=top_n)
            return [
                RetrievedChunk(chunk=chunks[rerank_pool[idx][0]], score=score, sources=sources[rerank_pool[idx][0]])
                for idx, score in ranked
            ]

        top = rerank_pool[:top_n]
        return [
            RetrievedChunk(chunk=chunks[chunk_id], score=score, sources=sources[chunk_id])
            for chunk_id, score in top
        ]

    def _safe_vector(self, query: str, kind: str | None) -> list[tuple[str, float, KBChunk]]:
        try:
            vector = self.embedder.encode([query])[0]
            return self.vectorstore.search(vector, self.cfg.vector_top_k, kind)
        except Exception as exc:  # 单路召回失败不应拖垮整条链路
            logger.warning("向量召回失败: %s", exc)
            return []

    def _safe_keyword(self, query: str, kind: str | None) -> list[tuple[str, float, KBChunk]]:
        try:
            return self.keywordstore.search(query, self.cfg.keyword_top_k, kind)
        except Exception as exc:
            logger.warning("关键词召回失败: %s", exc)
            return []
