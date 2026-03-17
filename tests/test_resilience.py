"""Tests for retry, circuit breaker, and rate limiting."""
import asyncio
import pytest
from skills.shared.resilience import (
    retry, RetryExhausted,
    CircuitBreaker, CircuitOpenError, CircuitState,
    RateLimiter,
    with_timeout,
)


class TestRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await succeed()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_retries(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("temporary failure")
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        @retry(max_attempts=3, base_delay=0.01)
        async def always_fail():
            raise ConnectionError("permanent failure")

        with pytest.raises(RetryExhausted) as exc_info:
            await always_fail()
        assert exc_info.value.attempts == 3

    @pytest.mark.asyncio
    async def test_retryable_exceptions_filter(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01, retryable_exceptions=(ConnectionError,))
        async def wrong_exception():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            await wrong_exception()
        assert call_count == 1  # No retries for ValueError


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_closed_by_default(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        assert cb.state == CircuitState.CLOSED

        @cb
        async def succeed():
            return "ok"

        assert await succeed() == "ok"

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=100)

        @cb
        async def fail():
            raise ConnectionError("down")

        for _ in range(3):
            with pytest.raises(ConnectionError):
                await fail()

        assert cb.state == CircuitState.OPEN

        # Now it should fast-fail
        with pytest.raises(CircuitOpenError):
            await fail()

    @pytest.mark.asyncio
    async def test_half_open_recovery(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=0.1)
        call_count = 0

        @cb
        async def sometimes_fail():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("down")
            return "recovered"

        # Trip the breaker
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await sometimes_fail()

        assert cb.state == CircuitState.OPEN

        # Wait for recovery timeout
        await asyncio.sleep(0.15)

        # Should transition to half-open and succeed
        result = await sometimes_fail()
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    def test_manual_reset(self):
        cb = CircuitBreaker(name="test", failure_threshold=1)
        cb.state = CircuitState.OPEN
        cb.failure_count = 10

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_stats(self):
        cb = CircuitBreaker(name="test-stats", failure_threshold=5)
        stats = cb.stats
        assert stats["name"] == "test-stats"
        assert stats["state"] == "closed"
        assert stats["failures"] == 0


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        limiter = RateLimiter(max_calls=5, period=1.0)
        for _ in range(5):
            await limiter.acquire()
        assert limiter.remaining == 0

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        limiter = RateLimiter(max_calls=2, period=0.2)
        await limiter.acquire()
        await limiter.acquire()

        # Third call should be delayed
        import time
        start = time.time()
        await limiter.acquire()
        elapsed = time.time() - start
        assert elapsed >= 0.1  # Had to wait


class TestTimeout:
    @pytest.mark.asyncio
    async def test_completes_within_timeout(self):
        async def fast():
            return "done"

        result = await with_timeout(fast(), timeout_seconds=1.0)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        async def slow():
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError, match="timed out"):
            await with_timeout(slow(), timeout_seconds=0.1, operation="slow_op")
