"""
Resilience patterns: retry with exponential backoff, circuit breaker,
timeout management, and rate limiting.
"""
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Callable, Optional
from collections import deque

from .config import get_logger

logger = get_logger("resilience")


# === Retry with Exponential Backoff ===

class RetryExhausted(Exception):
    """All retry attempts failed."""
    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(f"All {attempts} retry attempts failed: {last_exception}")


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (Exception,),
    on_retry: Optional[Callable] = None,
):
    """
    Decorator for async functions with exponential backoff retry.

    Usage:
        @retry(max_attempts=3, base_delay=1.0)
        async def fetch_prices():
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        break

                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    # Add jitter (±25%)
                    import random
                    jitter = delay * 0.25 * (2 * random.random() - 1)
                    delay = max(0.1, delay + jitter)

                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__}: "
                        f"{e}. Waiting {delay:.1f}s"
                    )

                    if on_retry:
                        on_retry(attempt, e, delay)

                    await asyncio.sleep(delay)

            raise RetryExhausted(last_exception, max_attempts)

        return wrapper
    return decorator


# === Circuit Breaker ===

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing — reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.

    When failures exceed threshold, the circuit "opens" and fast-fails
    all calls for a cooldown period. After cooldown, it allows one test
    call through ("half-open"). If that succeeds, circuit closes. If it
    fails, circuit opens again.

    Usage:
        binance_circuit = CircuitBreaker(name="binance", failure_threshold=5)

        @binance_circuit
        async def call_binance():
            ...
    """
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0  # seconds
    half_open_max_calls: int = 1

    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    failure_count: int = field(default=0, init=False)
    success_count: int = field(default=0, init=False)
    last_failure_time: float = field(default=0.0, init=False)
    half_open_calls: int = field(default=0, init=False)

    def __call__(self, func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not self._can_execute():
                raise CircuitOpenError(
                    f"Circuit '{self.name}' is OPEN — "
                    f"service unavailable, retry after "
                    f"{self._remaining_cooldown():.0f}s"
                )

            try:
                result = await func(*args, **kwargs)
                self._on_success()
                return result
            except Exception as e:
                self._on_failure()
                raise

        return wrapper

    def _can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                logger.info(f"Circuit '{self.name}': OPEN → HALF_OPEN")
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_calls < self.half_open_max_calls

        return False

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"Circuit '{self.name}': HALF_OPEN → CLOSED (recovered)")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
        self.success_count += 1

    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            logger.warning(f"Circuit '{self.name}': HALF_OPEN → OPEN (still failing)")
            self.state = CircuitState.OPEN
            return

        if self.failure_count >= self.failure_threshold:
            logger.warning(
                f"Circuit '{self.name}': CLOSED → OPEN "
                f"(failures: {self.failure_count}/{self.failure_threshold})"
            )
            self.state = CircuitState.OPEN

    def _remaining_cooldown(self) -> float:
        elapsed = time.time() - self.last_failure_time
        return max(0, self.recovery_timeout - elapsed)

    def reset(self):
        """Manually reset the circuit breaker."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        logger.info(f"Circuit '{self.name}': manually reset to CLOSED")

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failure_count,
            "successes": self.success_count,
            "cooldown_remaining": self._remaining_cooldown() if self.state == CircuitState.OPEN else 0,
        }


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open."""
    pass


# === Rate Limiter ===

class RateLimiter:
    """
    Token bucket rate limiter.

    Usage:
        limiter = RateLimiter(max_calls=60, period=60.0)  # 60 calls/min
        await limiter.acquire()  # blocks until a token is available
    """

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until rate limit allows another call."""
        async with self._lock:
            now = time.time()

            # Remove expired timestamps
            while self._calls and self._calls[0] <= now - self.period:
                self._calls.popleft()

            if len(self._calls) >= self.max_calls:
                # Wait until the oldest call expires
                wait_time = self._calls[0] + self.period - now
                if wait_time > 0:
                    logger.debug(f"Rate limited — waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
                self._calls.popleft()

            self._calls.append(time.time())

    @property
    def remaining(self) -> int:
        now = time.time()
        active = sum(1 for t in self._calls if t > now - self.period)
        return max(0, self.max_calls - active)


# === Timeout ===

async def with_timeout(coro, timeout_seconds: float, operation: str = "operation"):
    """Run a coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{operation} timed out after {timeout_seconds}s")


# === Pre-configured instances for fintech services ===

# Exchange circuit breakers
binance_circuit = CircuitBreaker(name="binance", failure_threshold=5, recovery_timeout=60)
coinbase_circuit = CircuitBreaker(name="coinbase", failure_threshold=5, recovery_timeout=60)
alchemy_circuit = CircuitBreaker(name="alchemy", failure_threshold=3, recovery_timeout=30)

# Rate limiters
exchange_limiter = RateLimiter(max_calls=20, period=60.0)  # 20 trades/min
api_limiter = RateLimiter(max_calls=60, period=60.0)       # 60 API calls/min
sec_limiter = RateLimiter(max_calls=10, period=60.0)       # SEC is rate-limited
