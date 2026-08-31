"""向量嵌入：默认本地 bge-m3（进程内加载），可切换 OpenAI 兼容端点（如 Qwen3-Embedding GGUF）。"""
from __future__ import annotations

import httpx

from reviewhive.config import EmbeddingConfig


class BaseEmbedder:
    dim: int = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


def _resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        return "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:
        return "cpu"


class SentenceTransformerEmbedder(BaseEmbedder):
    def __init__(self, cfg: EmbeddingConfig):
        from sentence_transformers import SentenceTransformer  # 惰性导入，避免基础安装强依赖

        if not cfg.model_path:
            raise ValueError("embedding.model_path 未配置")
        self._model = SentenceTransformer(cfg.model_path, device=_resolve_device(cfg.device))
        self._batch_size = cfg.batch_size
        self._normalize = cfg.normalize
        self._cache: dict[str, list[float]] = {}
        get_dim = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(get_dim() or 0)

    def encode(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = []
        to_encode: list[str] = []
        indices: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is not None:
                results.append(cached)
            else:
                results.append(None)
                to_encode.append(text)
                indices.append(i)
        if to_encode:
            vectors = self._model.encode(
                to_encode,
                batch_size=self._batch_size,
                normalize_embeddings=self._normalize,
                show_progress_bar=False,
            ).tolist()
            for idx, text, vec in zip(indices, to_encode, vectors):
                self._cache[text] = vec
                results[idx] = vec
        return results  # type: ignore[return-value]


class OpenAICompatEmbedder(BaseEmbedder):
    def __init__(self, cfg: EmbeddingConfig):
        if not cfg.base_url:
            raise ValueError("embedding.base_url 未配置（provider=openai 时必填）")
        self._client = httpx.Client(base_url=cfg.base_url, timeout=120)
        self._model = cfg.model
        self._cache: dict[str, list[float]] = {}
        self.dim = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = []
        to_encode: list[str] = []
        indices: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is not None:
                results.append(cached)
            else:
                results.append(None)
                to_encode.append(text)
                indices.append(i)
        if to_encode:
            resp = self._client.post("/embeddings", json={"model": self._model, "input": to_encode})
            resp.raise_for_status()
            data = resp.json()["data"]
            vectors = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
            if vectors and not self.dim:
                self.dim = len(vectors[0])
            for idx, text, vec in zip(indices, to_encode, vectors):
                self._cache[text] = vec
                results[idx] = vec
        return results  # type: ignore[return-value]


def build_embedder(cfg: EmbeddingConfig) -> BaseEmbedder:
    if cfg.provider == "openai":
        return OpenAICompatEmbedder(cfg)
    return SentenceTransformerEmbedder(cfg)
