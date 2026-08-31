"""通用 LRU 缓存：用于 LLM / Embedding / Vision 等模型调用的去重。"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any


class CallCache:
    """线程安全的 LRU 缓存，key 为 SHA-256 哈希字符串。"""

    def __init__(self, max_size: int = 512):
        self._max_size = max_size
        self._store: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    @staticmethod
    def make_key(*parts: Any) -> str:
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
