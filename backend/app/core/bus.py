"""Event bus used for live execution streaming and domain events.

Fan-out order: in-process subscribers always receive the event (so SSE/WebSocket
clients on this pod work with zero infrastructure), Redis pub/sub broadcasts it to
sibling pods when configured, and Kafka receives a durable copy when configured.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import orjson

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("bus")

MAX_QUEUE = 2000


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._redis: Any | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._kafka: Any | None = None
        self.kafka_connected = False

    async def connect(self) -> None:
        if settings.redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(settings.redis_url, decode_responses=False)
                await self._redis.ping()
                self._pubsub_task = asyncio.create_task(self._consume_redis())
                log.info("bus_redis_connected")
            except Exception as exc:
                self._redis = None
                log.warning("bus_redis_unavailable", error=str(exc))
        if settings.kafka_bootstrap_servers:
            try:
                from aiokafka import AIOKafkaProducer

                self._kafka = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
                await self._kafka.start()
                self.kafka_connected = True
                log.info("bus_kafka_connected")
            except Exception as exc:
                self._kafka = None
                log.warning("bus_kafka_unavailable", error=str(exc))

    async def close(self) -> None:
        if self._pubsub_task:
            self._pubsub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pubsub_task
        if self._redis is not None:
            await self._redis.aclose()
        if self._kafka is not None:
            await self._kafka.stop()

    async def _consume_redis(self) -> None:
        assert self._redis is not None
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe("finops:events:*")
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            try:
                channel = message["channel"].decode().split("finops:events:", 1)[1]
                payload = orjson.loads(message["data"])
                if payload.get("__origin") == id(self):
                    continue
                await self._local_publish(channel, payload)
            except Exception as exc:  # pragma: no cover
                log.warning("bus_redis_decode_failed", error=str(exc))

    async def _local_publish(self, channel: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._subs.get(channel, ())) + list(self._subs.get("*", ()))
        for q in queues:
            if q.qsize() >= MAX_QUEUE:
                continue
            q.put_nowait((channel, event))

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        await self._local_publish(channel, event)
        if self._redis is not None:
            try:
                await self._redis.publish(
                    f"finops:events:{channel}", orjson.dumps({**event, "__origin": id(self)})
                )
            except Exception as exc:
                log.warning("bus_publish_failed", error=str(exc))
        if self._kafka is not None:
            topic = f"{settings.kafka_topic_prefix}.{channel.split(':')[0]}"
            try:
                await self._kafka.send(topic, orjson.dumps(event))
            except Exception as exc:
                log.warning("bus_kafka_send_failed", topic=topic, error=str(exc))

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        async with self._lock:
            self._subs.setdefault(channel, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subs.get(channel, set()).discard(queue)
                if not self._subs.get(channel):
                    self._subs.pop(channel, None)

    async def stream(self, channel: str, *, heartbeat: float = 15.0) -> AsyncIterator[dict[str, Any]]:
        async with self.subscribe(channel) as queue:
            while True:
                try:
                    _, event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    yield event
                except TimeoutError:
                    yield {"type": "heartbeat"}

    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subs.values())


bus = EventBus()


def execution_channel(execution_id: str) -> str:
    return f"execution:{execution_id}"


LOG_CHANNEL = "logs:stream"
METRIC_CHANNEL = "metrics:stream"
APPROVAL_CHANNEL = "approvals:stream"
AGENT_CHANNEL = "agents:stream"
