"""向量库：Qdrant。payload 中完整保存 KBChunk，检索命中即自带内容。"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from reviewhive.core.schema import KBChunk

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, chunk_id))


class QdrantStore:
    def __init__(self, url: str, collection: str):
        self.collection = collection
        self._client = QdrantClient(url=url)

    def ensure(self, dim: int) -> None:
        if self._client.collection_exists(self.collection):
            return
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )

    def upsert(self, chunks: list[KBChunk], vectors: list[list[float]]) -> int:
        points = [
            qmodels.PointStruct(
                id=point_id(chunk.id),
                vector=vector,
                payload={"chunk": chunk.model_dump(), "chunk_id": chunk.id},
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self._client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, vector: list[float], top_k: int, kind: str | None = None) -> list[tuple[str, float, KBChunk]]:
        query_filter = None
        if kind:
            query_filter = qmodels.Filter(must=[qmodels.FieldCondition(key="chunk.kind", match=qmodels.MatchValue(value=kind))])
        hits = self._client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        ).points
        results: list[tuple[str, float, KBChunk]] = []
        for hit in hits:
            payload = hit.payload or {}
            try:
                chunk = KBChunk(**payload["chunk"])
            except Exception:
                continue
            results.append((payload.get("chunk_id", chunk.id), float(hit.score), chunk))
        return results

    def count(self) -> int:
        try:
            return int(self._client.count(collection_name=self.collection, exact=True).count)
        except Exception:
            return 0

    # ---------- 命名集合操作（长期记忆等复用） ----------

    def collection_exists(self, name: str) -> bool:
        return bool(self._client.collection_exists(name))

    def create_memory_collection(self, name: str, dim: int) -> None:
        self._client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )

    def upsert_memory(self, name: str, memory_id: str, vector: list[float], payload: dict) -> None:
        self._client.upsert(
            collection_name=name,
            points=[qmodels.PointStruct(id=point_id(memory_id), vector=vector, payload=payload)],
        )

    def delete_memory(self, name: str, memory_id: str) -> None:
        self._client.delete(collection_name=name, points_selector=qmodels.PointIdsList(points=[point_id(memory_id)]))

    def search_memory(self, name: str, vector: list[float], top_k: int) -> list[tuple[str, dict]]:
        if not self.collection_exists(name):
            return []
        hits = self._client.query_points(collection_name=name, query=vector, limit=top_k, with_payload=True).points
        results: list[tuple[str, dict]] = []
        for hit in hits:
            payload = hit.payload or {}
            results.append((payload.get("id", ""), payload))
        return results
