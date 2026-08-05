"""Celery application for scheduled and background work.

Only started when CELERY_BROKER_URL is configured; the API itself never depends on it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("worker")

celery_app = Celery(
    "finops",
    broker=settings.celery_broker_url or settings.redis_url or "memory://",
    backend=settings.celery_result_backend or settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "sync-knowledge-sources": {
            "task": "finops.sync_knowledge",
            "schedule": crontab(minute="0", hour="*/2"),
        },
        "transaction-monitoring": {
            "task": "finops.run_transaction_monitoring",
            "schedule": crontab(minute="15", hour="*"),
        },
        "expire-approvals": {
            "task": "finops.expire_approvals",
            "schedule": crontab(minute="*/15"),
        },
        "apply-retention": {
            "task": "finops.apply_retention",
            "schedule": crontab(minute="30", hour="3"),
        },
        "run-scheduled-agents": {
            "task": "finops.run_scheduled_agents",
            "schedule": crontab(minute="*"),
        },
    },
)


def _run(coro) -> Any:
    return asyncio.run(coro)


@celery_app.task(name="finops.sync_knowledge")
def sync_knowledge() -> dict[str, Any]:
    from sqlalchemy import select

    from app.db.models.knowledge import KnowledgeSource
    from app.db.session import session_scope
    from app.rag.connectors import CONNECTORS

    async def run() -> dict[str, Any]:
        results = []
        async with session_scope() as session:
            sources = (
                await session.execute(
                    select(KnowledgeSource).where(KnowledgeSource.enabled.is_(True))
                )
            ).scalars().all()
            for source in sources:
                connector = CONNECTORS.get(source.connector)
                if connector is None or not connector.configured:
                    continue
                results.append(await connector.sync(session, source))
            await session.commit()
        return {"synced": len(results), "results": results}

    return _run(run())


@celery_app.task(name="finops.run_transaction_monitoring")
def run_transaction_monitoring(days: int = 7) -> dict[str, Any]:
    from app.db.session import session_scope
    from app.tools import aml  # noqa: F401 - ensures registration
    from app.tools.base import ToolContext, registry

    async def run() -> dict[str, Any]:
        async with session_scope() as session:
            result = await registry.invoke(
                "monitor_transactions", {"days": days, "persist_alerts": True},
                ToolContext(agent_key="scheduled_monitoring", session=session, state={}),
            )
            await session.commit()
        return result.to_dict()

    return _run(run())


@celery_app.task(name="finops.expire_approvals")
def expire_approvals() -> dict[str, Any]:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db.models.agents import Approval, Execution
    from app.db.session import session_scope

    async def run() -> dict[str, Any]:
        now = datetime.now(UTC)
        expired = 0
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Approval).where(Approval.status == "pending",
                                           Approval.expires_at < now)
                )
            ).scalars().all()
            for approval in rows:
                approval.status = "expired"
                approval.timeline = [*(approval.timeline or []),
                                     {"at": now.isoformat(), "event": "expired"}]
                execution = (
                    await session.execute(
                        select(Execution).where(Execution.id == approval.execution_id)
                    )
                ).scalar_one_or_none()
                if execution and execution.status == "awaiting_approval":
                    execution.status = "cancelled"
                    execution.error = "Approval request expired"
                    execution.finished_at = now
                    execution.checkpoint = None
                expired += 1
            await session.commit()
        return {"expired": expired}

    return _run(run())


@celery_app.task(name="finops.apply_retention")
def apply_retention() -> dict[str, Any]:
    from app.cli import cmd_retention

    _run(cmd_retention(dry_run=False))
    return {"status": "completed"}


@celery_app.task(name="finops.run_scheduled_agents")
def run_scheduled_agents() -> dict[str, Any]:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db.models.agents import ScheduledJob
    from app.db.session import session_scope
    from app.engine.executor import engine

    async def run() -> dict[str, Any]:
        now = datetime.now(UTC)
        launched = []
        async with session_scope() as session:
            jobs = (
                await session.execute(
                    select(ScheduledJob).where(ScheduledJob.enabled.is_(True))
                )
            ).scalars().all()
            for job in jobs:
                if job.next_run_at and job.next_run_at > now:
                    continue
                execution = await engine.submit(
                    session, agent_key=job.agent_key, payload=job.payload, trigger="schedule"
                )
                job.last_run_at = now
                job.next_run_at = _next_cron_run(job.cron, now)
                launched.append({"job": job.name, "execution_id": execution.id})
            await session.commit()
        return {"launched": launched}

    return _run(run())


def _next_cron_run(expression: str, after):
    """Minimal 5-field cron evaluator returning the next matching minute."""
    from datetime import timedelta

    fields = expression.split()
    if len(fields) != 5:
        return after + timedelta(hours=1)

    def matches(value: int, spec: str) -> bool:
        if spec == "*":
            return True
        for part in spec.split(","):
            if part.startswith("*/"):
                if value % int(part[2:]) == 0:
                    return True
            elif "-" in part:
                start, end = (int(x) for x in part.split("-"))
                if start <= value <= end:
                    return True
            elif int(part) == value:
                return True
        return False

    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 8):
        if (matches(candidate.minute, fields[0]) and matches(candidate.hour, fields[1])
                and matches(candidate.day, fields[2]) and matches(candidate.month, fields[3])
                and matches((candidate.weekday() + 1) % 7, fields[4])):
            return candidate
        candidate += timedelta(minutes=1)
    return after + timedelta(days=1)
