"""主 LLM 客户端：对接 llama-server 的 OpenAI 兼容接口（openai SDK）。

使用官方 openai SDK 的意义：接口与 llama-server 完全兼容，同时让
openinference-instrumentation-openai 能自动采集每次调用的
prompt / 补全 / token / 延迟（见 observability.py）。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from openai import APIStatusError, AsyncOpenAI, BadRequestError, OpenAIError

from reviewhive.config import LLMConfig
from reviewhive.models.cache import CallCache


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """从模型输出中稳健地提取 JSON：支持裸 JSON、代码围栏与嵌入文本中的首个平衡对象。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
            start = text.find(opener, start + 1)
    raise ValueError(f"无法从模型输出解析 JSON: {text[:200]}...")


def build_openai_client(base_url: str, timeout_seconds: float) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=base_url,
        api_key="reviewhive-local",  # llama-server 不校验密钥，仅满足 SDK 要求
        timeout=timeout_seconds,
        max_retries=0,
    )


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = build_openai_client(cfg.base_url, cfg.timeout_seconds)
        self._cache = CallCache()

    async def close(self) -> None:
        await self._client.close()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        temp = self.cfg.temperature if temperature is None else temperature
        cache_key = CallCache.make_key(messages, temp, json_mode)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        params: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        try:
            completion = await self._client.chat.completions.create(**params)
        except BadRequestError:
            if not json_mode:
                raise LLMError("LLM 请求被拒绝（400）")
            # 个别服务不支持 response_format，退化为普通请求
            params.pop("response_format", None)
            try:
                completion = await self._client.chat.completions.create(**params)
            except OpenAIError as exc:
                raise LLMError(f"LLM 请求失败: {exc}") from exc
        except OpenAIError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        try:
            result = completion.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise LLMError(f"LLM 响应结构异常: {str(completion)[:300]}") from exc
        self._cache.set(cache_key, result)
        return result

    async def chat_json(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        raw = await self.chat(messages, json_mode=True, **kwargs)
        try:
            return extract_json(raw)
        except ValueError:
            # json_object 不被支持或模型仍输出杂质时退化为普通请求再解析一次
            raw = await self.chat(messages, **kwargs)
            return extract_json(raw)

    async def healthy(self) -> bool:
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
