"""多模态客户端：Qwen3-VL（llama-server --mmproj），用于读取架构图/截图。"""
from __future__ import annotations

import base64
import hashlib

from openai import APIStatusError, OpenAIError

from reviewhive.config import ResilienceConfig, VisionConfig
from reviewhive.models.cache import CallCache
from reviewhive.models.llm import build_openai_client
from reviewhive.resilience import CircuitBreaker, CircuitOpenError, CircuitState, RetryPolicy


_RETRYABLE_STATUS = {500, 502, 503, 504}


class VisionClient:
    def __init__(self, cfg: VisionConfig, resilience: ResilienceConfig | None = None):
        self.cfg = cfg
        self._client = build_openai_client(cfg.base_url, cfg.timeout_seconds)
        self._cache = CallCache()
        res = resilience or ResilienceConfig()
        self._breaker = CircuitBreaker(
            name="vision",
            failure_threshold=res.vision_cb.failure_threshold,
            recovery_timeout=res.vision_cb.recovery_timeout,
            success_threshold=res.vision_cb.success_threshold,
        )
        self._retry = RetryPolicy(
            max_retries=res.vision_retry.max_retries,
            base_delay=res.vision_retry.base_delay,
            max_delay=res.vision_retry.max_delay,
            retryable_exceptions=(TimeoutError, ConnectionError, OSError),
        )

    async def close(self) -> None:
        await self._client.close()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def is_available(self) -> bool:
        return self._breaker.state != CircuitState.OPEN

    async def describe(self, image_bytes: bytes, prompt: str, mime_type: str = "image/png") -> str:
        img_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        cache_key = CallCache.make_key(img_hash, prompt, mime_type)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                    },
                ],
            }
        ]

        async def _do_describe() -> str:
            try:
                completion = await self._breaker.call(
                    self._raw_describe, messages,
                )
                return completion
            except APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS:
                    raise ConnectionError(f"Vision 返回 {exc.status_code}") from exc
                raise

        try:
            result = await self._retry.execute(_do_describe)
        except CircuitOpenError as exc:
            raise RuntimeError(f"Vision 服务熔断中: {exc}") from exc
        except OpenAIError as exc:
            raise RuntimeError(f"Vision 请求失败: {exc}") from exc

        self._cache.set(cache_key, result)
        return result

    async def _raw_describe(self, messages: list[dict]) -> str:
        completion = await self._client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=0.1,
            max_tokens=2048,
            stream=False,
        )
        return completion.choices[0].message.content or ""

    async def healthy(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self.cfg.base_url.rstrip('/')}/models",
                    headers={"Authorization": "Bearer reviewhive-local"},
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
        except Exception:
            return False
