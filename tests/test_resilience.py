"""熔断器 + 重试器单元测试。"""
from __future__ import annotations

import asyncio
import time

import pytest

from reviewhive.resilience import CircuitBreaker, CircuitOpenError, CircuitState, RetryPolicy


# ---------- CircuitBreaker ----------


class TestCircuitBreakerStateTransitions:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == CircuitState.CLOSED

    def test_closed_to_open_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count_in_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_open_to_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed_after_success_threshold(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, success_threshold=2)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, success_threshold=2)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED


class TestCircuitBreakerCall:
    @pytest.mark.asyncio
    async def test_call_success(self):
        cb = CircuitBreaker(failure_threshold=3)

        async def ok_fn():
            return "ok"

        result = await cb.call(ok_fn)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_call_failure_records(self):
        cb = CircuitBreaker(failure_threshold=2)

        async def fail_fn():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_call_raises_circuit_open_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        async def fail_fn():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await cb.call(fail_fn)  # triggers OPEN

        async def ok_fn():
            return "ok"

        with pytest.raises(CircuitOpenError):
            await cb.call(ok_fn)

    def test_call_sync_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        result = cb.call_sync(lambda: 42)
        assert result == 42

    def test_call_sync_failure_records(self):
        cb = CircuitBreaker(failure_threshold=2)

        def fail_fn():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            cb.call_sync(fail_fn)
        with pytest.raises(RuntimeError):
            cb.call_sync(fail_fn)
        assert cb.state == CircuitState.OPEN

    def test_call_sync_raises_circuit_open_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        def fail_fn():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            cb.call_sync(fail_fn)
        with pytest.raises(CircuitOpenError):
            cb.call_sync(lambda: "ok")


# ---------- RetryPolicy ----------


class TestRetryPolicy:
    @pytest.mark.asyncio
    async def test_execute_succeeds_first_try(self):
        retry = RetryPolicy(max_retries=3, base_delay=0.01)
        calls = 0

        async def ok_fn():
            nonlocal calls
            calls += 1
            return "done"

        result = await retry.execute(ok_fn)
        assert result == "done"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_execute_retries_on_retryable_exception(self):
        retry = RetryPolicy(max_retries=2, base_delay=0.01, retryable_exceptions=(TimeoutError,))
        calls = 0

        async def flaky_fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimeoutError("timeout")
            return "ok"

        result = await retry.execute(flaky_fn)
        assert result == "ok"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_execute_gives_up_after_max_retries(self):
        retry = RetryPolicy(max_retries=2, base_delay=0.01, retryable_exceptions=(TimeoutError,))
        calls = 0

        async def always_fail():
            nonlocal calls
            calls += 1
            raise TimeoutError("timeout")

        with pytest.raises(TimeoutError):
            await retry.execute(always_fail)
        assert calls == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_execute_does_not_retry_non_retryable(self):
        retry = RetryPolicy(max_retries=3, base_delay=0.01, retryable_exceptions=(TimeoutError,))
        calls = 0

        async def value_error_fn():
            nonlocal calls
            calls += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            await retry.execute(value_error_fn)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_execute_passes_circuit_open_error_without_retry(self):
        retry = RetryPolicy(max_retries=3, base_delay=0.01)
        calls = 0

        async def circuit_open_fn():
            nonlocal calls
            calls += 1
            raise CircuitOpenError("open")

        with pytest.raises(CircuitOpenError):
            await retry.execute(circuit_open_fn)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_backoff_delay_increases(self):
        retry = RetryPolicy(max_retries=3, base_delay=0.1, max_delay=10.0, jitter=False)
        delays = [retry._compute_delay(i) for i in range(3)]
        assert delays[0] == pytest.approx(0.1, abs=0.01)
        assert delays[1] == pytest.approx(0.2, abs=0.01)
        assert delays[2] == pytest.approx(0.4, abs=0.01)

    @pytest.mark.asyncio
    async def test_backoff_respects_max_delay(self):
        retry = RetryPolicy(max_retries=5, base_delay=1.0, max_delay=2.0, jitter=False)
        delay = retry._compute_delay(10)
        assert delay == 2.0


# ---------- 组合使用 ----------


class TestRetryWithCircuitBreaker:
    @pytest.mark.asyncio
    async def test_retry_wraps_breaker(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        retry = RetryPolicy(max_retries=3, base_delay=0.01, retryable_exceptions=(ConnectionError,))
        calls = 0

        async def broken_fn():
            nonlocal calls
            calls += 1
            raise ConnectionError("conn failed")

        async def wrapped():
            return await cb.call(broken_fn)

        # Attempt 1: ConnectionError (breaker: 1 failure)
        # Attempt 2: ConnectionError (breaker: 2 failures → OPEN)
        # Attempt 3: CircuitOpenError → retry re-raises
        with pytest.raises(CircuitOpenError):
            await retry.execute(wrapped)
        assert calls == 2  # only 2 actual calls before breaker opened
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_open_stops_retry_immediately(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        retry = RetryPolicy(max_retries=5, base_delay=0.01, retryable_exceptions=(ConnectionError,))
        calls = 0

        async def fail_fn():
            nonlocal calls
            calls += 1
            raise ConnectionError("fail")

        async def wrapped():
            return await cb.call(fail_fn)

        # Attempt 1: ConnectionError (breaker: 1 failure → OPEN)
        # Attempt 2: CircuitOpenError → retry re-raises immediately
        with pytest.raises(CircuitOpenError):
            await retry.execute(wrapped)
        assert calls == 1  # only 1 actual call before breaker opened
        assert cb.state == CircuitState.OPEN
