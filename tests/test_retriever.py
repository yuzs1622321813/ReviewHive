from reviewhive.config import RAGConfig
from reviewhive.core.schema import KBChunk
from reviewhive.models.embedding import BaseEmbedder
from reviewhive.rag.retriever import HybridRetriever


class FakeEmbedder(BaseEmbedder):
    dim = 4

    def encode(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeQdrant:
    def __init__(self, hits):
        self._hits = hits

    def search(self, vector, top_k, kind=None):
        return self._hits[:top_k]


class FakeES:
    def __init__(self, hits):
        self._hits = hits

    def search(self, query, top_k, kind=None):
        return self._hits[:top_k]


def _chunk(chunk_id: str, content: str) -> KBChunk:
    return KBChunk(id=chunk_id, source="test", title=chunk_id, content=content)


def test_rrf_fusion_prefers_dual_hits():
    shared = ("c1", 0.9, _chunk("c1", "双路命中"))
    qdrant_hits = [shared, ("c2", 0.8, _chunk("c2", "仅向量"))]
    es_hits = [shared, ("c3", 5.0, _chunk("c3", "仅关键词"))]
    retriever = HybridRetriever(RAGConfig(), FakeQdrant(qdrant_hits), FakeES(es_hits), FakeEmbedder(), reranker=None)

    results = retriever.retrieve("任意查询", top_n=3)
    assert [hit.chunk.id for hit in results][0] == "c1"
    assert len(results) == 3
    c1 = next(hit for hit in results if hit.chunk.id == "c1")
    assert set(c1.sources) == {"vector", "keyword"}


def test_single_source_failure_degrades_gracefully():
    class BrokenQdrant:
        def search(self, vector, top_k, kind=None):
            raise RuntimeError("qdrant down")

    es_hits = [("c3", 5.0, _chunk("c3", "仅关键词"))]
    retriever = HybridRetriever(RAGConfig(), BrokenQdrant(), FakeES(es_hits), FakeEmbedder(), reranker=None)
    results = retriever.retrieve("查询", top_n=5)
    assert [hit.chunk.id for hit in results] == ["c3"]


def test_empty_results():
    retriever = HybridRetriever(RAGConfig(), FakeQdrant([]), FakeES([]), FakeEmbedder(), reranker=None)
    assert retriever.retrieve("查询") == []
