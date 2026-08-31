"""重排：本地 bge-reranker-v2-m3（cross-encoder，进程内加载）。"""
from __future__ import annotations

from reviewhive.config import RerankerConfig
from reviewhive.models.embedding import _resolve_device


class CrossEncoderReranker:
    def __init__(self, cfg: RerankerConfig):
        from sentence_transformers import CrossEncoder  # 惰性导入

        if not cfg.model_path:
            raise ValueError("reranker.model_path 未配置")
        self._model = CrossEncoder(cfg.model_path, device=_resolve_device(cfg.device), max_length=512)
        self.top_n = cfg.top_n

    def rank(self, query: str, docs: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
        """返回按分数降序的 (原始索引, 分数) 列表，截断到 top_n。"""
        if not docs:
            return []
        pairs = [(query, doc) for doc in docs]
        scores = self._model.predict(pairs)
        ordered = sorted(enumerate(map(float, scores)), key=lambda item: item[1], reverse=True)
        return ordered[: (top_n or self.top_n)]


def build_reranker(cfg: RerankerConfig) -> CrossEncoderReranker | None:
    if not cfg.enabled:
        return None
    return CrossEncoderReranker(cfg)
