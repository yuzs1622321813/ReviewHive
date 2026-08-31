"""配置加载：config/settings.yaml（可被 settings.local.yaml 与环境变量覆盖）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppConfig(BaseModel):
    name: str = "ReviewHive"
    host: str = "0.0.0.0"
    port: int = 9100
    data_dir: str = "./data"
    db_path: str = "./data/reviewhive.db"


class LLMConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "qwen3.6-35b-a3b"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_seconds: float = 300


class VisionConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8082/v1"
    model: str = "qwen3-vl-8b"
    timeout_seconds: float = 180


class EmbeddingConfig(BaseModel):
    provider: str = "sentence_transformer"  # 或 openai（OpenAI 兼容端点）
    model_path: str = ""
    base_url: str = ""
    model: str = ""
    device: str = "auto"
    batch_size: int = 16
    normalize: bool = True


class RerankerConfig(BaseModel):
    enabled: bool = True
    model_path: str = ""
    device: str = "auto"
    top_n: int = 8


class ModelsConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    vision: VisionConfig = VisionConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    reranker: RerankerConfig = RerankerConfig()


class ChunkerConfig(BaseModel):
    strategy: str = "heading"  # heading（结构优先）| fixed_window（定长滑窗）
    max_chars: int = 1500
    overlap: int = 150


class RAGConfig(BaseModel):
    qdrant_url: str = "http://127.0.0.1:6333"
    collection: str = "reviewhive_kb"
    es_url: str = "http://127.0.0.1:9200"
    es_index: str = "reviewhive_kb"
    vector_top_k: int = 20
    keyword_top_k: int = 20
    rrf_k: int = 60
    chunker: ChunkerConfig = ChunkerConfig()


class ReviewConfig(BaseModel):
    max_skill_loops: int = 4
    max_input_chars: int = 40000
    sub_agents: list[str] = ["security", "performance", "style", "test"]


class ObservabilityConfig(BaseModel):
    enabled: bool = True
    phoenix_url: str = "http://127.0.0.1:6006"
    project: str = "reviewhive"


class MemoryConfig(BaseModel):
    enabled: bool = True
    collection: str = "reviewhive_memory"   # Qdrant 记忆向量集合
    recall_top_k: int = 4                   # 每次评审注入的记忆条数
    lesson_ttl_days: int = 90               # lesson 类记忆未使用多久后归档
    archive_retention_days: int = 30        # 归档后再保留多久删除
    compress_threshold: int = 60            # 活跃记忆超过该数量触发压缩
    compress_similarity: float = 0.85       # 余弦相似度高于该值判定为可合并
    summarize_threshold: int = 40           # lesson 超过该数量触发摘要蒸馏
    summarize_batch: int = 12               # 每次蒸馏的最老 lesson 数
    maintain_every_sessions: int = 5        # 每 N 次评审执行一次生命周期维护
    dedup_similarity: float = 0.9           # 写入新记忆时去重的余弦相似度阈值


class ProjectReviewConfig(BaseModel):
    max_files: int = 10                 # LLM 深审文件上限
    min_score: int = 3                  # 触发深审的最低风险分
    concurrency: int = 2                # 并行深审数（本地模型不宜过高）
    max_scan_files: int = 2000          # 静态扫描文件数上限
    exclude: list[str] = [".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "build", "dist", ".eggs"]
    context_limit: int = 6000           # 注入单文件评审的接口上下文字符上限
    emit_scanner_findings: bool = True  # 高精度静态模式是否产出 Finding


class Settings(BaseModel):
    app: AppConfig = AppConfig()
    models: ModelsConfig = ModelsConfig()
    rag: RAGConfig = RAGConfig()
    review: ReviewConfig = ReviewConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    memory: MemoryConfig = MemoryConfig()
    project: ProjectReviewConfig = ProjectReviewConfig()


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_paths(settings: Settings) -> Settings:
    for cfg in (settings.models.embedding, settings.models.reranker):
        if cfg.model_path:
            cfg.model_path = str(Path(cfg.model_path).expanduser())
    settings.app.data_dir = str(Path(settings.app.data_dir).expanduser())
    settings.app.db_path = str(Path(settings.app.db_path).expanduser())
    return settings


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    llm_url = os.environ.get("REVIEWHIVE_LLM_BASE_URL")
    if llm_url:
        data.setdefault("models", {}).setdefault("llm", {})["base_url"] = llm_url
    return data


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path or os.environ.get("REVIEWHIVE_CONFIG") or PROJECT_ROOT / "config" / "settings.yaml")
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local_path = path.with_name("settings.local.yaml")
    if local_path.exists():
        data = _deep_merge(data, yaml.safe_load(local_path.read_text(encoding="utf-8")) or {})
    data = _apply_env_overrides(data)
    settings = Settings(**data)
    if not Path(settings.app.db_path).is_absolute():
        settings.app.db_path = str(PROJECT_ROOT / settings.app.db_path)
    if not Path(settings.app.data_dir).is_absolute():
        settings.app.data_dir = str(PROJECT_ROOT / settings.app.data_dir)
    return _expand_paths(settings)
