"""Cache adapter: Redis when configured, in-process LRU/TTL otherwise."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import cache_events_total

log = get_logger("cache")


class Cache:
    def __init__(self) -> None:
        self._redis: Any | None = None
        self._local: dict[str, tuple[float, bytes]] = {}
        self._lock = asyncio.Lock()
        self.backend = "memory"

    async def connect(self) -> None:
        if not settings.redis_url:
            log.info("cache_backend", backend="memory", reason="redis_url_not_set")
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.redis_url, encoding=None, decode_responses=False)
            await self._redis.ping()
            self.backend = "redis"
            log.info("cache_backend", backend="redis")
        except Exception as exc:
            self._redis = None
            log.warning("redis_unavailable_falling_back", error=str(exc))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def healthy(self) -> bool:
        return self._redis is not None

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False

    async def get(self, key: str, *, scope: str = "default") -> Any | None:
        raw: bytes | None = None
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
            except Exception as exc:
                log.warning("cache_get_failed", key=key, error=str(exc))
        else:
            async with self._lock:
                entry = self._local.get(key)
                if entry and entry[0] > time.time():
                    raw = entry[1]
                elif entry:
                    self._local.pop(key, None)
        cache_events_total.labels(scope=scope, event="hit" if raw else "miss").inc()
        return orjson.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        raw = orjson.dumps(value)
        if self._redis is not None:
            try:
                await self._redis.set(key, raw, ex=ttl)
                return
            except Exception as exc:
                log.warning("cache_set_failed", key=key, error=str(exc))
        async with self._lock:
            if len(self._local) > 10_000:
                now = time.time()
                for k, (exp, _) in list(self._local.items()):
                    if exp <= now:
                        self._local.pop(k, None)
            self._local[key] = (time.time() + ttl, raw)

    async def delete(self, *keys: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(*keys)
                return
            except Exception:
                pass
        async with self._lock:
            for k in keys:
                self._local.pop(k, None)

    async def incr(self, key: str, amount: int = 1, ttl: int = 3600) -> int:
        if self._redis is not None:
            try:
                value = await self._redis.incrby(key, amount)
                await self._redis.expire(key, ttl)
                return int(value)
            except Exception:
                pass
        current = await self.get(key) or 0
        current = int(current) + amount
        await self.set(key, current, ttl)
        return current

    async def stats(self) -> dict[str, Any]:
        if self._redis is not None:
            try:
                info = await self._redis.info("stats")
                mem = await self._redis.info("memory")
                hits = int(info.get("keyspace_hits", 0))
                misses = int(info.get("keyspace_misses", 0))
                return {
                    "backend": "redis",
                    "hits": hits,
                    "misses": misses,
                    "hit_rate": round(hits / (hits + misses), 4) if hits + misses else 0.0,
                    "used_memory_bytes": int(mem.get("used_memory", 0)),
                    "connected": True,
                }
            except Exception as exc:
                return {"backend": "redis", "connected": False, "error": str(exc)}
        return {"backend": "memory", "connected": True, "entries": len(self._local)}


cache = Cache()
