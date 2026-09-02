"""关键词库：Elasticsearch BM25，兜住向量检索对精确符号/标识符召回弱的问题。"""
from __future__ import annotations

import logging

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from reviewhive.core.schema import KBChunk
from reviewhive.resilience import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

_MAPPING = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "title": {"type": "text"},
        "content": {"type": "text"},
        "code": {"type": "text"},
        "kind": {"type": "keyword"},
        "source": {"type": "keyword"},
        "language": {"type": "keyword"},
        "chunk": {"type": "object", "enabled": False},
    }
}


class ESStore:
    def __init__(self, url: str, index: str, breaker: CircuitBreaker | None = None):
        self.index = index
        self._client = Elasticsearch(url, request_timeout=60, max_retries=0)
        self._breaker = breaker

    def ensure(self) -> None:
        if not self._client.indices.exists(index=self.index):
            self._client.indices.create(index=self.index, mappings=_MAPPING)

    def index_chunks(self, chunks: list[KBChunk]) -> int:
        actions = [
            {
                "_index": self.index,
                "_id": chunk.id,
                "_source": {
                    "chunk_id": chunk.id,
                    "title": chunk.title,
                    "content": chunk.content,
                    "code": chunk.code,
                    "kind": chunk.kind,
                    "source": chunk.source,
                    "language": chunk.language,
                    "chunk": chunk.model_dump(),
                },
            }
            for chunk in chunks
        ]
        success, _ = bulk(self._client, actions, refresh=False)
        self._client.indices.refresh(index=self.index)
        return int(success)

    def search(self, query: str, top_k: int, kind: str | None = None) -> list[tuple[str, float, KBChunk]]:
        if self._breaker is None:
            return self._raw_search(query, top_k, kind)
        try:
            return self._breaker.call_sync(self._raw_search, query, top_k, kind)
        except CircuitOpenError:
            logger.warning("[%s] ES 熔断器打开，跳过关键词检索", self._breaker.name)
            return []

    def _raw_search(self, query: str, top_k: int, kind: str | None = None) -> list[tuple[str, float, KBChunk]]:
        must: list[dict] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["title^2", "content", "code^0.5"],
                    "type": "best_fields",
                }
            }
        ]
        if kind:
            must.append({"term": {"kind": kind}})
        resp = self._client.search(index=self.index, query={"bool": {"must": must}}, size=top_k)
        results: list[tuple[str, float, KBChunk]] = []
        for hit in resp["hits"]["hits"]:
            source = hit["_source"]
            try:
                chunk = KBChunk(**source["chunk"])
            except Exception:
                continue
            results.append((source.get("chunk_id", chunk.id), float(hit["_score"]), chunk))
        return results

    def count(self) -> int:
        try:
            return int(self._client.count(index=self.index)["count"])
        except Exception:
            return 0
