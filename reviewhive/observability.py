"""链路追踪（可选）：OpenTelemetry + OpenInference 语义 → Arize Phoenix。

设计原则：
- 完全可选。开关关闭、依赖未安装或 Phoenix 不可达时，全部退化为 no-op，
  评审主流程不受任何影响；
- LLM 调用由 openinference-instrumentation-openai 自动埋点（prompt/补全/
  token/延迟），编排链路由 span() 助手手动埋点。
"""
from __future__ import annotations

import atexit
import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# OpenInference 语义属性（无 semconv 包时退化为字面量字符串，Phoenix 同样识别）
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
KIND_CHAIN = "CHAIN"
KIND_AGENT = "AGENT"
KIND_TOOL = "TOOL"
KIND_RETRIEVER = "RETRIEVER"

_tracer: Any = None


def setup(cfg) -> bool:
    """初始化 TracerProvider 并注册 openai 自动埋点。返回是否启用成功。"""
    global _tracer
    if not cfg.enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("未安装可观测依赖（pip install 'reviewhive[observability]'），链路追踪已禁用")
        return False

    endpoint = cfg.phoenix_url.rstrip("/") + "/v1/traces"
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "reviewhive",
                "openinference.project.name": cfg.project,
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    atexit.register(provider.force_flush)

    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument(tracer_provider=provider)
    except Exception as exc:
        logger.warning("openai 自动埋点不可用（LLM 调用将无 span）: %s", exc)

    _tracer = trace.get_tracer("reviewhive")
    logger.info("链路追踪已启用 -> %s（项目：%s）", endpoint, cfg.project)
    return True


@contextmanager
def span(name: str, kind: str | None = None, attributes: dict[str, Any] | None = None):
    """埋点上下文管理器；追踪未启用时为透明 no-op。"""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as active:
        if kind:
            active.set_attribute(SPAN_KIND, kind)
        for key, value in (attributes or {}).items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                active.set_attribute(key, value)
            else:
                active.set_attribute(key, str(value))
        yield active
