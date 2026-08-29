"""Cost ledger sink and tool health recorder wired into the router and tool registry.

Both writers join the caller's transaction when one is ambient (set by the execution
engine). That keeps metering inside the same unit of work as the execution it belongs to
and avoids a second connection contending for the write lock.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.agents import CostRecord, ToolHealth
from app.db.session import ambient_session, session_scope
from app.tools.base import ToolResult, registry

log = get_logger("telemetry")

MAX_SAMPLES = 200


def _build_cost_record(record: dict[str, Any]) -> CostRecord:
    return CostRecord(
        timestamp=datetime.now(UTC),
        execution_id=record.get("execution_id"),
        span_id=record.get("span_id"),
        agent_key=record.get("agent_key"),
        category=record.get("category", "llm"),
        provider=record.get("provider", "unknown"),
        model=record.get("model"),
        tool=record.get("tool"),
        tokens_input=int(record.get("tokens_input", 0)),
        tokens_output=int(record.get("tokens_output", 0)),
        tokens_cached=int(record.get("tokens_cached", 0)),
        cost_usd=float(record.get("cost_usd", 0.0)),
        user_id=record.get("user_id"),
        user_email=record.get("user_email"),
        department=record.get("department"),
        latency_ms=record.get("latency_ms"),
    )


async def record_cost(record: dict[str, Any]) -> None:
    """Persist one metered LLM/embedding call to the cost ledger."""
    try:
        session = ambient_session.get()
        if session is not None:
            session.add(_build_cost_record(record))
            return
        async with session_scope() as own_session:
            own_session.add(_build_cost_record(record))
    except Exception as exc:  # pragma: no cover - metering must never break a call
        log.warning("cost_record_failed", error=str(exc))


async def _update_tool_health(session: AsyncSession, tool_name: str, result: ToolResult, status: str) -> None:
    row = (
        await session.execute(select(ToolHealth).where(ToolHealth.tool_name == tool_name))
    ).scalar_one_or_none()
    if row is None:
        category = registry.get(tool_name).category if registry.has(tool_name) else "unknown"
        row = ToolHealth(
            tool_name=tool_name,
            category=category,
            total_calls=0,
            total_failures=0,
            total_timeouts=0,
            total_retries=0,
            p50_latency_ms=0.0,
            p95_latency_ms=0.0,
            last_latency_ms=0.0,
            latency_samples=[],
        )
        session.add(row)

    row.total_calls = (row.total_calls or 0) + 1
    row.total_retries = (row.total_retries or 0) + result.retries
    if not result.ok:
        row.total_failures = (row.total_failures or 0) + 1
        row.last_error = (result.error or "")[:2000]
    if status == "timeout":
        row.total_timeouts = (row.total_timeouts or 0) + 1
    row.last_status = status
    row.last_latency_ms = result.latency_ms
    row.last_called_at = datetime.now(UTC)

    samples = [s["ms"] for s in (row.latency_samples or [])][-MAX_SAMPLES + 1 :]
    samples.append(result.latency_ms)
    row.latency_samples = [{"ms": round(s, 3)} for s in samples]
    ordered = sorted(samples)
    row.p50_latency_ms = statistics.median(ordered)
    row.p95_latency_ms = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]
    await session.flush()


async def record_tool_health(tool_name: str, result: ToolResult, status: str) -> None:
    try:
        ambient = ambient_session.get()
        if ambient is not None:
            await _update_tool_health(ambient, tool_name, result, status)
            return
        async with session_scope() as session:
            await _update_tool_health(session, tool_name, result, status)
    except Exception as exc:  # pragma: no cover
        log.warning("tool_health_record_failed", tool=tool_name, error=str(exc))


def install() -> None:
    from app.llm.router import router

    router.register_cost_sink(record_cost)
    registry.set_health_hook(record_tool_health)
