"""Retry, circuit breaker, bulkhead and token-bucket rate limiting."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core.errors import CircuitOpenError, RateLimitError, is_transient
from app.core.logging import get_logger
from app.core.metrics import circuit_state

log = get_logger("resilience")
T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.4
    max_delay: float = 8.0
    jitter: float = 0.25
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    give_up_on: tuple[type[BaseException], ...] = ()
    #: Consult :func:`is_transient` before spending an attempt. Type membership alone cannot
    #: separate a 401 from a 503 -- both arrive as ``ProviderError`` -- and retrying the 401
    #: costs the full ladder to arrive at the same rejection.
    only_if_transient: bool = True

    def should_retry(self, exc: BaseException) -> bool:
        return is_transient(exc) if self.only_if_transient else True

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (1 + random.uniform(-self.jitter, self.jitter))


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy | None = None,
    *,
    on_retry: Callable[[int, BaseException], None] | None = None,
    name: str = "operation",
) -> T:
    policy = policy or RetryPolicy()
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except policy.give_up_on:
            raise
        except policy.retry_on as exc:  # type: ignore[misc]
            last = exc
            if attempt >= policy.max_attempts or not policy.should_retry(exc):
                break
            if on_retry:
                on_retry(attempt, exc)
            delay = policy.delay_for(attempt)
            log.warning("retrying", operation=name, attempt=attempt, delay=round(delay, 3), error=str(exc))
            await asyncio.sleep(delay)
    assert last is not None
    raise last


class CircuitBreaker:
    """Standard three-state breaker (closed -> open -> half-open)."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        half_open_max_calls: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.half_open_max_calls = half_open_max_calls
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()
        self.total_failures = 0
        self.total_successes = 0
        self.total_rejections = 0

    @property
    def state(self) -> str:
        return self._state

    def _publish(self) -> None:
        circuit_state.labels(name=self.name).set({"closed": 0, "half_open": 1, "open": 2}[self._state])

    async def _before(self) -> None:
        async with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.recovery_seconds:
                    self._state = "half_open"
                    self._half_open_calls = 0
                    self._publish()
                else:
                    self.total_rejections += 1
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is open",
                        details={
                            "retry_after_seconds": round(
                                self.recovery_seconds - (time.monotonic() - self._opened_at), 2
                            )
                        },
                    )
            if self._state == "half_open":
                if self._half_open_calls >= self.half_open_max_calls:
                    self.total_rejections += 1
                    raise CircuitOpenError(f"Circuit '{self.name}' is recovering")
                self._half_open_calls += 1

    async def _success(self) -> None:
        async with self._lock:
            self.total_successes += 1
            self._failures = 0
            if self._state in ("half_open", "open"):
                self._state = "closed"
            self._publish()

    async def _failure(self) -> None:
        async with self._lock:
            self.total_failures += 1
            self._failures += 1
            if self._state == "half_open" or self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                log.error("circuit_opened", circuit=self.name, failures=self._failures)
            self._publish()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        await self._before()
        try:
            result = await fn()
        except CircuitOpenError:
            raise
        except Exception as exc:
            # A breaker measures whether the dependency is reachable, not whether the caller
            # got what it wanted. A rejected key answers the first question with "yes" -- and
            # counting it as an outage replaces the one error message that says how to fix it
            # with `Circuit is open` for every caller until the recovery window elapses.
            if is_transient(exc):
                await self._failure()
            else:
                async with self._lock:
                    self.total_failures += 1
            raise
        await self._success()
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state,
            "consecutive_failures": self._failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "total_rejections": self.total_rejections,
            "failure_threshold": self.failure_threshold,
            "recovery_seconds": self.recovery_seconds,
        }


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name, **kwargs)
        _breakers[name]._publish()
    return _breakers[name]


def all_breakers() -> list[dict[str, Any]]:
    return [b.snapshot() for b in _breakers.values()]


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class TokenBucketLimiter:
    """In-process limiter; backed by Redis when the cache adapter is distributed."""

    def __init__(self, rate_per_minute: int, burst: int | None = None):
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst or rate_per_minute)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = bucket
            bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, bucket.tokens
            retry_after = (cost - bucket.tokens) / self.rate
            return False, retry_after

    async def enforce(self, key: str, cost: float = 1.0) -> None:
        ok, info = await self.check(key, cost)
        if not ok:
            raise RateLimitError(
                "Rate limit exceeded", details={"retry_after_seconds": round(info, 2), "key": key}
            )


class Bulkhead:
    """Caps concurrent work of a class (e.g. agent executions)."""

    def __init__(self, name: str, limit: int):
        self.name = name
        self.limit = limit
        self._sem = asyncio.Semaphore(limit)
        self.in_flight = 0

    async def __aenter__(self) -> Bulkhead:
        await self._sem.acquire()
        self.in_flight += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.in_flight -= 1
        self._sem.release()
