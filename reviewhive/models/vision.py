"""多模态客户端：Qwen3-VL（llama-server --mmproj），用于读取架构图/截图。"""
from __future__ import annotations

import base64
import hashlib

from openai import OpenAIError

from reviewhive.config import VisionConfig
from reviewhive.models.cache import CallCache
from reviewhive.models.llm import build_openai_client


class VisionClient:
    def __init__(self, cfg: VisionConfig):
        self.cfg = cfg
        self._client = build_openai_client(cfg.base_url, cfg.timeout_seconds)
        self._cache = CallCache()

    async def close(self) -> None:
        await self._client.close()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

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
        try:
            completion = await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
                stream=False,
            )
        except OpenAIError as exc:
            raise RuntimeError(f"Vision 请求失败: {exc}") from exc
        result = completion.choices[0].message.content or ""
        self._cache.set(cache_key, result)
        return result

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
