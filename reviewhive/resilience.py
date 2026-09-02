"""熔断器 + 重试器：为外部依赖提供弹性保护。"""
from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """熔断器处于 OPEN 状态时抛出，表示快速失败。"""


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """三态熔断器。

    CLOSED  → 连续失败达到 threshold → OPEN
    OPEN    → 超过 recovery_timeout   → HALF_OPEN
    HALF_OPEN → 连续成功达到 threshold → CLOSED
    HALF_OPEN → 任意一次失败           → OPEN（重新计时）
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
        name: str = "",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.name = name or "breaker"

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info("[%s] 熔断器 OPEN → HALF_OPEN", self.name)
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    def record_success(self) -> None:
        state = self.state
        if state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info("[%s] 熔断器 HALF_OPEN → CLOSED（恢复）", self.name)
        elif state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        state = self.state
        if state == CircuitState.HALF_OPEN:
            self._trip()
        elif state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._success_count = 0
        logger.warning("[%s] 熔断器 → OPEN（连续失败 %d 次）", self.name, self._failure_count)

    async def call(self, coro_fn: Callable[..., Awaitable], *args: Any, **kwargs: Any) -> Any:
        if self.is_open:
            raise CircuitOpenError(f"[{self.name}] 熔断器打开，快速失败")
        try:
            result = await coro_fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure()
            raise

    def call_sync(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """同步版本的 call，用于 Qdrant/ES 等同步客户端。"""
        if self.is_open:
            raise CircuitOpenError(f"[{self.name}] 熔断器打开，快速失败")
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure()
            raise

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = 0.0


@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 10.0
    retryable_exceptions: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError)
    jitter: bool = True

    async def execute(self, coro_fn: Callable[..., Awaitable], *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except CircuitOpenError:
                raise
            except tuple(self.retryable_exceptions) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self._compute_delay(attempt)
                    logger.info("重试 %d/%d（%.1fs 后）: %s", attempt + 1, self.max_retries, delay, exc)
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _compute_delay(self, attempt: int) -> float:
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return delay
