"""Executive overview, AI usage, cost analytics and platform metrics."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.api.deps import PrincipalDep, SessionDep
from app.core.cache import cache
from app.core.config import settings
from app.core.metrics import collect_runtime_gauges
from app.core.rbac import Permission
from app.core.resilience import all_breakers
from app.db.models.agents import (
    Agent,
    Approval,
    CostRecord,
    Evaluation,
    Execution,
    LogRecord,
    ToolHealth,
)
from app.db.models.knowledge import Chunk, Document, KnowledgeSource, SearchQueryLog
from app.engine.executor import bulkhead
from app.llm.catalog import CATALOG

dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])
cost_router = APIRouter(prefix="/costs", tags=["costs"])
metrics_router = APIRouter(prefix="/platform-metrics", tags=["metrics"])


def _read_cpu_percent() -> float:
    try:
        load = os.getloadavg()[0]
        return round(min(load / (os.cpu_count() or 1) * 100, 100.0), 2)
    except OSError:
        return 0.0


def _read_memory() -> dict[str, float]:
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                values[key] = int(rest.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        used = total - available
        return {"total_bytes": total, "used_bytes": used,
                "used_pct": round(used / total * 100, 2) if total else 0.0}
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "used_pct": 0.0}


def _read_network() -> dict[str, int]:
    rx = tx = 0
    try:
        with open("/proc/net/dev") as fh:
            for line in fh.readlines()[2:]:
                iface, _, data = line.partition(":")
                if iface.strip() == "lo":
                    continue
                parts = data.split()
                rx += int(parts[0])
                tx += int(parts[8])
    except OSError:
        pass
    return {"rx_bytes": rx, "tx_bytes": tx}


def _read_gpu() -> list[dict[str, Any]]:
    import shutil
    import subprocess

    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        gpus = []
        for line in out.stdout.strip().splitlines():
            idx, name, util, mem_used, mem_total, temp = (p.strip() for p in line.split(","))
            gpus.append({"index": int(idx), "name": name, "utilisation_pct": float(util),
                         "memory_used_mb": float(mem_used), "memory_total_mb": float(mem_total),
                         "temperature_c": float(temp)})
        return gpus
    except Exception:
        return []


@dashboard_router.get("/overview")
async def executive_overview(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.METRIC_READ)
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    window_24h = now - timedelta(hours=24)

    agent_rows = (await session.execute(select(Agent))).scalars().all()
    implemented = [a for a in agent_rows if a.availability == "implemented"]

    status_counts = dict(
        (await session.execute(
            select(Execution.status, func.count(Execution.id))
            .where(Execution.created_at >= window_24h).group_by(Execution.status)
        )).all()
    )
    live_counts = dict(
        (await session.execute(
            select(Execution.status, func.count(Execution.id))
            .where(Execution.status.in_(["running", "queued", "awaiting_approval"]))
            .group_by(Execution.status)
        )).all()
    )

    totals = (
        await session.execute(
            select(
                func.count(Execution.id),
                func.avg(Execution.latency_ms),
                func.sum(Execution.cost_usd),
                func.sum(Execution.tokens_input),
                func.sum(Execution.tokens_output),
                func.sum(Execution.tokens_cached),
                func.sum(Execution.retry_count),
            ).where(Execution.created_at >= window_24h)
        )
    ).one()
    count_24h, avg_latency, cost_24h, tin, tout, tcached, retries = totals

    all_time = int((await session.execute(select(func.count(Execution.id)))).scalar_one())
    daily_cost = float(
        (await session.execute(
            select(func.sum(CostRecord.cost_usd)).where(CostRecord.timestamp >= day_start)
        )).scalar_one() or 0.0
    )
    monthly_cost = float(
        (await session.execute(
            select(func.sum(CostRecord.cost_usd)).where(CostRecord.timestamp >= month_start)
        )).scalar_one() or 0.0
    )
    errors_today = int(
        (await session.execute(
            select(func.count(LogRecord.id)).where(
                LogRecord.timestamp >= day_start, LogRecord.level.in_(["ERROR", "CRITICAL"])
            )
        )).scalar_one()
    )
    approvals = dict(
        (await session.execute(
            select(Approval.status, func.count(Approval.id)).group_by(Approval.status)
        )).all()
    )

    succeeded = status_counts.get("succeeded", 0)
    failed = status_counts.get("failed", 0) + status_counts.get("timeout", 0)
    finished = succeeded + failed
    memory = _read_memory()
    collect_runtime_gauges()

    return {
        "generated_at": now.isoformat(),
        "agents": {
            "total": len(agent_rows),
            "implemented": len(implemented),
            "coming_soon": len(agent_rows) - len(implemented),
            "active": sum(1 for a in implemented if a.lifecycle_state == "active"),
            "paused": sum(1 for a in implemented if a.lifecycle_state == "paused"),
            "disabled": sum(1 for a in implemented if a.lifecycle_state == "disabled"),
            "running": live_counts.get("running", 0),
            "queued": live_counts.get("queued", 0),
            "awaiting_approval": live_counts.get("awaiting_approval", 0),
            "idle": sum(1 for a in implemented if a.lifecycle_state == "active")
            - live_counts.get("running", 0),
            "failed_24h": failed,
        },
        "executions": {
            "total_all_time": all_time,
            "last_24h": int(count_24h or 0),
            "succeeded_24h": succeeded,
            "failed_24h": failed,
            "cancelled_24h": status_counts.get("cancelled", 0),
            "success_rate_pct": round(succeeded / finished * 100, 2) if finished else 100.0,
            "avg_latency_ms": int(avg_latency or 0),
            "avg_cost_usd": round(float(cost_24h or 0) / count_24h, 6) if count_24h else 0.0,
            "retries_24h": int(retries or 0),
        },
        "tokens": {
            "input_24h": int(tin or 0),
            "output_24h": int(tout or 0),
            "cached_24h": int(tcached or 0),
            "total_24h": int((tin or 0) + (tout or 0)),
        },
        "cost": {
            "today_usd": round(daily_cost, 4),
            "month_to_date_usd": round(monthly_cost, 4),
            "daily_budget_usd": settings.daily_cost_budget_usd,
            "monthly_budget_usd": settings.monthly_cost_budget_usd,
            "daily_budget_used_pct": round(daily_cost / settings.daily_cost_budget_usd * 100, 2)
            if settings.daily_cost_budget_usd else 0.0,
            "monthly_budget_used_pct": round(
                monthly_cost / settings.monthly_cost_budget_usd * 100, 2)
            if settings.monthly_cost_budget_usd else 0.0,
        },
        "approvals": {
            "pending": approvals.get("pending", 0),
            "approved": approvals.get("approved", 0),
            "rejected": approvals.get("rejected", 0),
            "expired": approvals.get("expired", 0),
        },
        "errors_today": errors_today,
        "resources": {
            "cpu_percent": _read_cpu_percent(),
            "cpu_count": os.cpu_count(),
            "memory": memory,
            "network": _read_network(),
            "gpu": _read_gpu(),
            "queue_depth": live_counts.get("queued", 0),
            "in_flight_executions": bulkhead.in_flight,
            "concurrency_limit": bulkhead.limit,
        },
    }


@dashboard_router.get("/ai-usage")
async def ai_usage(session: SessionDep, principal: PrincipalDep,
                   days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    principal.require(Permission.METRIC_READ)
    since = datetime.now(UTC) - timedelta(days=days)
    by_model = (
        await session.execute(
            select(
                CostRecord.model, CostRecord.provider, CostRecord.category,
                func.count(CostRecord.id), func.sum(CostRecord.tokens_input),
                func.sum(CostRecord.tokens_output), func.sum(CostRecord.tokens_cached),
                func.sum(CostRecord.cost_usd), func.avg(CostRecord.latency_ms),
            ).where(CostRecord.timestamp >= since)
            .group_by(CostRecord.model, CostRecord.provider, CostRecord.category)
        )
    ).all()

    models = []
    total_input = total_output = total_cached = 0
    total_cost = 0.0
    total_calls = 0
    for model, provider, category, calls, tin, tout, tcached, cost, latency in by_model:
        spec = CATALOG.get(model or "")
        models.append({
            "model": model,
            "display_name": spec.display_name if spec else model,
            "provider": provider,
            "category": category,
            "calls": int(calls or 0),
            "tokens_input": int(tin or 0),
            "tokens_output": int(tout or 0),
            "tokens_cached": int(tcached or 0),
            "cost_usd": round(float(cost or 0), 6),
            "avg_latency_ms": round(float(latency or 0), 2),
        })
        total_input += int(tin or 0)
        total_output += int(tout or 0)
        total_cached += int(tcached or 0)
        total_cost += float(cost or 0)
        total_calls += int(calls or 0)
    models.sort(key=lambda m: m["cost_usd"], reverse=True)

    embedding_tokens = sum(m["tokens_input"] for m in models if m["category"] == "embedding")
    cache_stats = await cache.stats()
    search_stats = (
        await session.execute(
            select(func.count(SearchQueryLog.id), func.avg(SearchQueryLog.latency_ms))
            .where(SearchQueryLog.timestamp >= since)
        )
    ).one()

    return {
        "window_days": days,
        "totals": {
            "calls": total_calls,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cached_tokens": total_cached,
            "embedding_tokens": embedding_tokens,
            "total_tokens": total_input + total_output,
            "cost_usd": round(total_cost, 6),
            "cache_hit_rate_pct": round(total_cached / total_input * 100, 2) if total_input else 0.0,
        },
        "models": models,
        "distribution": [
            {"model": m["model"], "share_pct": round(m["calls"] / total_calls * 100, 2)}
            for m in models if total_calls
        ],
        "cache": cache_stats,
        "vector_search": {
            "queries": int(search_stats[0] or 0),
            "avg_latency_ms": round(float(search_stats[1] or 0), 2),
        },
    }


@dashboard_router.get("/timeseries")
async def execution_timeseries(session: SessionDep, principal: PrincipalDep,
                               hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
    principal.require(Permission.METRIC_READ)
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(Execution.created_at, Execution.status, Execution.latency_ms,
                   Execution.cost_usd, Execution.tokens_input, Execution.tokens_output)
            .where(Execution.created_at >= since).order_by(Execution.created_at)
        )
    ).all()
    bucket_minutes = 60 if hours > 6 else 5
    buckets: dict[str, dict[str, Any]] = {}
    for created_at, status_value, latency, cost, tin, tout in rows:
        stamp = created_at.replace(second=0, microsecond=0)
        stamp = stamp.replace(minute=(stamp.minute // bucket_minutes) * bucket_minutes)
        key = stamp.isoformat()
        bucket = buckets.setdefault(key, {"timestamp": key, "total": 0, "succeeded": 0,
                                          "failed": 0, "latency_sum": 0, "latency_count": 0,
                                          "cost_usd": 0.0, "tokens": 0})
        bucket["total"] += 1
        if status_value == "succeeded":
            bucket["succeeded"] += 1
        elif status_value in {"failed", "timeout"}:
            bucket["failed"] += 1
        if latency:
            bucket["latency_sum"] += latency
            bucket["latency_count"] += 1
        bucket["cost_usd"] += float(cost or 0)
        bucket["tokens"] += int((tin or 0) + (tout or 0))
    series = []
    for bucket in sorted(buckets.values(), key=lambda b: b["timestamp"]):
        series.append({
            "timestamp": bucket["timestamp"],
            "total": bucket["total"],
            "succeeded": bucket["succeeded"],
            "failed": bucket["failed"],
            "avg_latency_ms": round(bucket["latency_sum"] / bucket["latency_count"], 2)
            if bucket["latency_count"] else 0,
            "cost_usd": round(bucket["cost_usd"], 6),
            "tokens": bucket["tokens"],
        })
    return {"window_hours": hours, "bucket_minutes": bucket_minutes, "series": series}


# --- costs -------------------------------------------------------------------
@cost_router.get("/summary")
async def cost_summary(session: SessionDep, principal: PrincipalDep,
                       days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    principal.require(Permission.COST_READ)
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)

    async def total(after: datetime) -> float:
        return float(
            (await session.execute(
                select(func.sum(CostRecord.cost_usd)).where(CostRecord.timestamp >= after)
            )).scalar_one() or 0.0
        )

    daily, weekly, monthly = await total(day_start), await total(week_start), await total(month_start)

    def group(column: Any, label: str):
        return select(column, func.sum(CostRecord.cost_usd), func.count(CostRecord.id),
                      func.sum(CostRecord.tokens_input + CostRecord.tokens_output)) \
            .where(CostRecord.timestamp >= since).group_by(column)

    dimensions: dict[str, list[dict[str, Any]]] = {}
    for label, column in [
        ("agent", CostRecord.agent_key), ("model", CostRecord.model),
        ("provider", CostRecord.provider), ("user", CostRecord.user_email),
        ("department", CostRecord.department), ("tool", CostRecord.tool),
        ("category", CostRecord.category),
    ]:
        rows = (await session.execute(group(column, label))).all()
        dimensions[label] = sorted(
            [
                {"key": key or "unattributed", "cost_usd": round(float(cost or 0), 6),
                 "calls": int(calls or 0), "tokens": int(tokens or 0)}
                for key, cost, calls, tokens in rows
            ],
            key=lambda r: r["cost_usd"], reverse=True,
        )

    daily_rows = (
        await session.execute(
            select(func.date(CostRecord.timestamp), func.sum(CostRecord.cost_usd),
                   func.count(CostRecord.id))
            .where(CostRecord.timestamp >= since).group_by(func.date(CostRecord.timestamp))
        )
    ).all()
    daily_series = sorted(
        [{"date": str(d), "cost_usd": round(float(c or 0), 6), "calls": int(n or 0)}
         for d, c, n in daily_rows],
        key=lambda r: r["date"],
    )

    recent = [d["cost_usd"] for d in daily_series[-7:]]
    run_rate = sum(recent) / len(recent) if recent else 0.0
    days_remaining = (month_start.replace(month=month_start.month % 12 + 1, day=1)
                      - now).days if month_start.month != 12 else (
        month_start.replace(year=month_start.year + 1, month=1, day=1) - now).days

    alerts = []
    if settings.daily_cost_budget_usd and daily > settings.daily_cost_budget_usd * 0.8:
        alerts.append({"severity": "warning" if daily < settings.daily_cost_budget_usd
                       else "critical", "scope": "daily",
                       "message": f"Daily spend ${daily:.2f} against budget "
                                  f"${settings.daily_cost_budget_usd:.2f}"})
    forecast = monthly + run_rate * max(days_remaining, 0)
    if settings.monthly_cost_budget_usd and forecast > settings.monthly_cost_budget_usd:
        alerts.append({"severity": "warning", "scope": "monthly_forecast",
                       "message": f"Forecast ${forecast:.2f} exceeds monthly budget "
                                  f"${settings.monthly_cost_budget_usd:.2f}"})

    return {
        "window_days": days,
        "totals": {"today_usd": round(daily, 6), "week_usd": round(weekly, 6),
                   "month_usd": round(monthly, 6),
                   "window_usd": round(sum(d["cost_usd"] for d in daily_series), 6)},
        "budgets": {"daily_usd": settings.daily_cost_budget_usd,
                    "monthly_usd": settings.monthly_cost_budget_usd,
                    "per_execution_cap_usd": settings.per_execution_cost_cap_usd},
        "forecast": {"daily_run_rate_usd": round(run_rate, 6),
                     "month_end_projection_usd": round(forecast, 6),
                     "days_remaining_in_month": max(days_remaining, 0)},
        "by_agent": dimensions["agent"],
        "by_model": dimensions["model"],
        "by_provider": dimensions["provider"],
        "by_user": dimensions["user"],
        "by_department": dimensions["department"],
        "by_tool": dimensions["tool"],
        "by_category": dimensions["category"],
        "daily_series": daily_series,
        "alerts": alerts,
    }


@cost_router.get("/records")
async def cost_records(session: SessionDep, principal: PrincipalDep,
                       limit: int = Query(default=100, ge=1, le=1000),
                       agent_key: str | None = None) -> list[dict[str, Any]]:
    principal.require(Permission.COST_READ)
    stmt = select(CostRecord).order_by(CostRecord.timestamp.desc()).limit(limit)
    if agent_key:
        stmt = stmt.where(CostRecord.agent_key == agent_key)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {"timestamp": r.timestamp.isoformat(), "execution_id": r.execution_id,
         "agent_key": r.agent_key, "category": r.category, "provider": r.provider,
         "model": r.model, "tool": r.tool, "tokens_input": r.tokens_input,
         "tokens_output": r.tokens_output, "cost_usd": round(r.cost_usd, 8),
         "user_email": r.user_email, "department": r.department,
         "latency_ms": r.latency_ms}
        for r in rows
    ]


# --- platform metrics ---------------------------------------------------------
@metrics_router.get("")
async def platform_metrics(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.METRIC_READ)
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    minute_ago = now - timedelta(minutes=1)

    executions_last_hour = int(
        (await session.execute(
            select(func.count(Execution.id)).where(Execution.created_at >= hour_ago)
        )).scalar_one()
    )
    tokens_last_minute = int(
        (await session.execute(
            select(func.sum(Execution.tokens_input + Execution.tokens_output))
            .where(Execution.created_at >= minute_ago)
        )).scalar_one() or 0
    )
    latencies = (
        await session.execute(
            select(Execution.latency_ms).where(
                Execution.latency_ms.is_not(None), Execution.created_at >= hour_ago
            ).order_by(Execution.latency_ms)
        )
    ).scalars().all()

    def percentile(values: list[int], pct: float) -> int:
        if not values:
            return 0
        index = min(int(len(values) * pct), len(values) - 1)
        return int(values[index])

    errors = int(
        (await session.execute(
            select(func.count(LogRecord.id)).where(
                LogRecord.timestamp >= hour_ago, LogRecord.level.in_(["ERROR", "CRITICAL"])
            )
        )).scalar_one()
    )
    queue = int(
        (await session.execute(
            select(func.count(Execution.id)).where(Execution.status == "queued")
        )).scalar_one()
    )
    retries = int(
        (await session.execute(
            select(func.sum(Execution.retry_count)).where(Execution.created_at >= hour_ago)
        )).scalar_one() or 0
    )
    cost_hour = float(
        (await session.execute(
            select(func.sum(CostRecord.cost_usd)).where(CostRecord.timestamp >= hour_ago)
        )).scalar_one() or 0.0
    )
    memory = _read_memory()
    return {
        "generated_at": now.isoformat(),
        "throughput": {
            "requests_per_second": round(executions_last_hour / 3600, 4),
            "executions_last_hour": executions_last_hour,
            "tokens_per_second": round(tokens_last_minute / 60, 2),
        },
        "latency_ms": {
            "p50": percentile(list(latencies), 0.5),
            "p90": percentile(list(latencies), 0.9),
            "p95": percentile(list(latencies), 0.95),
            "p99": percentile(list(latencies), 0.99),
            "samples": len(latencies),
        },
        "errors_last_hour": errors,
        "queue_depth": queue,
        "retries_last_hour": retries,
        "cost_last_hour_usd": round(cost_hour, 6),
        "cpu_percent": _read_cpu_percent(),
        "memory": memory,
        "gpu": _read_gpu(),
        "network": _read_network(),
        "circuit_breakers": all_breakers(),
        "concurrency": {"in_flight": bulkhead.in_flight, "limit": bulkhead.limit},
    }


@metrics_router.get("/tools")
async def tool_metrics(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.METRIC_READ)
    rows = (await session.execute(select(ToolHealth).order_by(ToolHealth.tool_name))).scalars().all()
    return [
        {
            "tool": r.tool_name, "category": r.category, "total_calls": r.total_calls,
            "failures": r.total_failures, "timeouts": r.total_timeouts, "retries": r.total_retries,
            "success_rate_pct": round((r.total_calls - r.total_failures) / r.total_calls * 100, 2)
            if r.total_calls else 100.0,
            "p50_latency_ms": round(r.p50_latency_ms, 2),
            "p95_latency_ms": round(r.p95_latency_ms, 2),
            "last_latency_ms": round(r.last_latency_ms, 2),
            "last_status": r.last_status, "last_error": r.last_error,
            "last_called_at": r.last_called_at.isoformat() if r.last_called_at else None,
        }
        for r in rows
    ]


@metrics_router.get("/evaluations")
async def evaluation_metrics(session: SessionDep, principal: PrincipalDep,
                             days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    principal.require(Permission.EVAL_READ)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                Evaluation.agent_key,
                func.count(Evaluation.id),
                func.avg(Evaluation.faithfulness),
                func.avg(Evaluation.groundedness),
                func.avg(Evaluation.hallucination_score),
                func.avg(Evaluation.citation_score),
                func.avg(Evaluation.tool_success_rate),
                func.avg(Evaluation.answer_relevance),
                func.avg(Evaluation.latency_ms),
                func.avg(Evaluation.cost_usd),
                func.avg(Evaluation.human_rating),
                func.sum(case((Evaluation.human_rating.is_not(None), 1), else_=0)),
            ).where(Evaluation.created_at >= since).group_by(Evaluation.agent_key)
        )
    ).all()
    return {
        "window_days": days,
        "agents": [
            {
                "agent_key": agent_key, "evaluations": int(n or 0),
                "faithfulness": round(float(f or 0), 4),
                "groundedness": round(float(g or 0), 4),
                "hallucination_score": round(float(h or 0), 4),
                "citation_score": round(float(c or 0), 4),
                "tool_success_rate": round(float(t or 0), 4),
                "answer_relevance": round(float(a or 0), 4),
                "avg_latency_ms": round(float(lat or 0), 2),
                "avg_cost_usd": round(float(cost or 0), 6),
                "human_rating": round(float(hr), 2) if hr else None,
                "human_feedback_count": int(hc or 0),
            }
            for agent_key, n, f, g, h, c, t, a, lat, cost, hr, hc in rows
        ],
    }


@dashboard_router.get("/knowledge")
async def knowledge_dashboard(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.KNOWLEDGE_READ)
    from app.rag.connectors import connector_status
    from app.rag.vectorstore import get_vector_store

    sources = (await session.execute(select(KnowledgeSource))).scalars().all()
    documents = int((await session.execute(select(func.count(Document.id)))).scalar_one())
    chunks = int((await session.execute(select(func.count(Chunk.id)))).scalar_one())
    embedded = int(
        (await session.execute(
            select(func.count(Chunk.id)).where(Chunk.embedding.is_not(None))
        )).scalar_one()
    )
    dims = (
        await session.execute(select(Chunk.dimensions).where(Chunk.dimensions > 0).limit(1))
    ).scalar_one_or_none()
    search = (
        await session.execute(
            select(func.count(SearchQueryLog.id), func.avg(SearchQueryLog.latency_ms),
                   func.avg(SearchQueryLog.result_count))
            .where(SearchQueryLog.timestamp >= datetime.now(UTC) - timedelta(days=7))
        )
    ).one()
    store = get_vector_store()
    return {
        "sources": [
            {
                "key": s.key, "name": s.name, "connector": s.connector, "status": s.status,
                "enabled": s.enabled, "documents": s.document_count, "chunks": s.chunk_count,
                "embedding_model": s.embedding_model, "vector_dimensions": s.vector_dimensions,
                "classification": s.classification, "last_error": s.last_error,
                "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
            }
            for s in sources
        ],
        "connectors": connector_status(),
        "corpus": {
            "documents": documents, "chunks": chunks, "embedded_chunks": embedded,
            "embedding_coverage_pct": round(embedded / chunks * 100, 2) if chunks else 0.0,
            "vector_dimensions": dims or 0, "vector_backend": store.backend,
        },
        "search": {
            "queries_7d": int(search[0] or 0),
            "avg_latency_ms": round(float(search[1] or 0), 2),
            "avg_results": round(float(search[2] or 0), 2),
        },
    }


# --- geography ---------------------------------------------------------------
def _risk_level(code: str, *, watchlist: int) -> str:
    """Jurisdiction risk, derived from the AML rule set and the loaded watchlists."""
    from app.tools.aml import HIGH_RISK_COUNTRIES

    if code in HIGH_RISK_COUNTRIES:
        return "high"
    if watchlist:
        return "elevated"
    return "standard"


@dashboard_router.get("/geography")
async def geography_dashboard(session: SessionDep, principal: PrincipalDep,
                              days: int = Query(default=90, ge=1, le=730)) -> dict[str, Any]:
    """Where the book actually sits: transaction corridors, customer locations and
    jurisdiction risk, aggregated from the banking ledger over the requested window."""
    principal.require(Permission.METRIC_READ)
    from app.db.models.banking import (
        AmlAlert,
        Customer,
        SanctionsEntry,
        Security,
        Transaction,
    )
    from app.seed import geo_reference as geo

    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    amount = func.abs(Transaction.amount)

    country_rows = (
        await session.execute(
            select(
                Transaction.country,
                func.count(Transaction.id),
                func.sum(amount),
                func.sum(case((Transaction.direction == "credit", amount), else_=0.0)),
                func.sum(case((Transaction.direction == "debit", amount), else_=0.0)),
                func.sum(case((Transaction.is_flagged.is_(True), 1), else_=0)),
                func.count(func.distinct(Transaction.customer_id)),
            )
            .where(Transaction.booked_at >= since)
            .group_by(Transaction.country)
        )
    ).all()

    watchlist_rows = (
        await session.execute(
            select(SanctionsEntry.nationality, func.count(SanctionsEntry.id),
                   func.sum(case((SanctionsEntry.is_pep.is_(True), 1), else_=0)))
            .where(SanctionsEntry.nationality.is_not(None))
            .group_by(SanctionsEntry.nationality)
        )
    ).all()
    watchlist: dict[str, dict[str, int]] = {}
    for nationality, total, pep in watchlist_rows:
        code = geo.resolve(nationality)
        if code is None:
            continue
        bucket = watchlist.setdefault(code, {"entries": 0, "peps": 0})
        bucket["entries"] += int(total or 0)
        bucket["peps"] += int(pep or 0)

    security_rows = (
        await session.execute(
            select(Security.country, func.count(Security.id)).group_by(Security.country)
        )
    ).all()
    securities = {
        code: int(total)
        for code, total in ((geo.resolve(raw), value) for raw, value in security_rows)
        if code
    }

    customer_country_rows = (
        await session.execute(
            select(Customer.nationality, func.count(Customer.id))
            .group_by(Customer.nationality)
        )
    ).all()
    customers_by_country: dict[str, int] = {}
    for nationality, total in customer_country_rows:
        code = geo.resolve(nationality)
        if code:
            customers_by_country[code] = customers_by_country.get(code, 0) + int(total)

    total_value = sum(float(row[2] or 0.0) for row in country_rows)
    total_transactions = sum(int(row[1] or 0) for row in country_rows)

    countries: list[dict[str, Any]] = []
    unmapped: list[str] = []
    cross_border_value = cross_border_transactions = 0.0
    high_risk_value = high_risk_transactions = 0.0
    for code, count, value, inbound, outbound, flagged, customer_count in country_rows:
        resolved = geo.resolve(code)
        place = geo.country(resolved)
        if place is None or resolved is None:
            unmapped.append(str(code))
            continue
        marks = watchlist.get(resolved, {"entries": 0, "peps": 0})
        risk = _risk_level(resolved, watchlist=marks["entries"])
        value = float(value or 0.0)
        count = int(count or 0)
        if resolved != geo.DOMESTIC_COUNTRY:
            cross_border_value += value
            cross_border_transactions += count
        if risk == "high":
            high_risk_value += value
            high_risk_transactions += count
        countries.append({
            "code": resolved,
            "name": place.name,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "region": place.region,
            "domestic": resolved == geo.DOMESTIC_COUNTRY,
            "risk_level": risk,
            "transactions": count,
            "total_value": round(value, 2),
            "inbound_value": round(float(inbound or 0.0), 2),
            "outbound_value": round(float(outbound or 0.0), 2),
            "flagged": int(flagged or 0),
            "counterparty_customers": int(customer_count or 0),
            "resident_customers": customers_by_country.get(resolved, 0),
            "securities": securities.get(resolved, 0),
            "watchlist_entries": marks["entries"],
            "watchlist_peps": marks["peps"],
            "share_pct": round(value / total_value * 100, 2) if total_value else 0.0,
        })
    countries.sort(key=lambda item: item["total_value"], reverse=True)

    # Cities come from the customer master; the figures are that city's customers' ledger.
    city_rows = (
        await session.execute(
            select(
                Customer.address_city,
                func.count(func.distinct(Customer.id)),
                func.count(Transaction.id),
                func.sum(amount),
                func.sum(case((Transaction.country != geo.DOMESTIC_COUNTRY, amount), else_=0.0)),
                func.sum(case((Transaction.is_flagged.is_(True), 1), else_=0)),
            )
            .select_from(Customer)
            .outerjoin(
                Transaction,
                (Transaction.customer_id == Customer.id) & (Transaction.booked_at >= since),
            )
            .where(Customer.address_city.is_not(None))
            .group_by(Customer.address_city)
        )
    ).all()
    high_risk_customers = dict(
        (
            await session.execute(
                select(Customer.address_city, func.count(Customer.id))
                .where(Customer.address_city.is_not(None), Customer.risk_rating == "high")
                .group_by(Customer.address_city)
            )
        ).all()
    )
    alerts_by_city = dict(
        (
            await session.execute(
                select(Customer.address_city, func.count(AmlAlert.id))
                .select_from(AmlAlert)
                .join(Customer, Customer.id == AmlAlert.customer_id)
                .where(AmlAlert.detected_at >= since, Customer.address_city.is_not(None))
                .group_by(Customer.address_city)
            )
        ).all()
    )

    cities: list[dict[str, Any]] = []
    for name, customer_count, txn_count, value, cross_value, flagged in city_rows:
        place = geo.city(name)
        if place is None:
            unmapped.append(str(name))
            continue
        value = float(value or 0.0)
        cities.append({
            "name": place.name,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "country": place.region,
            "customers": int(customer_count or 0),
            "high_risk_customers": int(high_risk_customers.get(name, 0)),
            "transactions": int(txn_count or 0),
            "total_value": round(value, 2),
            "cross_border_value": round(float(cross_value or 0.0), 2),
            "flagged": int(flagged or 0),
            "alerts": int(alerts_by_city.get(name, 0)),
            "share_pct": round(value / total_value * 100, 2) if total_value else 0.0,
        })
    cities.sort(key=lambda item: item["total_value"], reverse=True)

    # Corridors: the customer's home city to the counterparty jurisdiction.
    corridor_rows = (
        await session.execute(
            select(
                Customer.address_city,
                Transaction.country,
                func.count(Transaction.id),
                func.sum(amount),
                func.sum(case((Transaction.direction == "debit", amount), else_=0.0)),
                func.sum(case((Transaction.is_flagged.is_(True), 1), else_=0)),
            )
            .select_from(Transaction)
            .join(Customer, Customer.id == Transaction.customer_id)
            .where(
                Transaction.booked_at >= since,
                Transaction.country != geo.DOMESTIC_COUNTRY,
                Customer.address_city.is_not(None),
            )
            .group_by(Customer.address_city, Transaction.country)
        )
    ).all()
    corridors: list[dict[str, Any]] = []
    for city_name, code, count, value, outbound, flagged in corridor_rows:
        origin = geo.city(city_name)
        resolved = geo.resolve(code)
        destination = geo.country(resolved)
        if origin is None or destination is None or resolved is None:
            continue
        value = float(value or 0.0)
        outbound = float(outbound or 0.0)
        corridors.append({
            "from_city": origin.name,
            "from_latitude": origin.latitude,
            "from_longitude": origin.longitude,
            "to_code": resolved,
            "to_country": destination.name,
            "to_latitude": destination.latitude,
            "to_longitude": destination.longitude,
            "risk_level": _risk_level(
                resolved, watchlist=watchlist.get(resolved, {}).get("entries", 0)),
            "transactions": int(count or 0),
            "total_value": round(value, 2),
            "outbound_value": round(outbound, 2),
            "inbound_value": round(value - outbound, 2),
            "flagged": int(flagged or 0),
        })
    corridors.sort(key=lambda item: item["total_value"], reverse=True)

    # Daily split of domestic against cross-border value.
    trend_rows = (
        await session.execute(
            select(Transaction.booked_at, Transaction.country, amount)
            .where(Transaction.booked_at >= since)
        )
    ).all()
    buckets: dict[str, dict[str, Any]] = {}
    for booked_at, code, value in trend_rows:
        key = booked_at.date().isoformat()
        bucket = buckets.setdefault(
            key, {"date": key, "transactions": 0, "domestic_value": 0.0,
                  "cross_border_value": 0.0, "high_risk_value": 0.0})
        bucket["transactions"] += 1
        resolved = geo.resolve(code) or ""
        value = float(value or 0.0)
        if resolved == geo.DOMESTIC_COUNTRY:
            bucket["domestic_value"] += value
        else:
            bucket["cross_border_value"] += value
        if _risk_level(resolved, watchlist=0) == "high":
            bucket["high_risk_value"] += value
    trend = [
        {**bucket,
         "domestic_value": round(bucket["domestic_value"], 2),
         "cross_border_value": round(bucket["cross_border_value"], 2),
         "high_risk_value": round(bucket["high_risk_value"], 2)}
        for bucket in sorted(buckets.values(), key=lambda b: b["date"])
    ]

    alerts = int(
        (await session.execute(
            select(func.count(AmlAlert.id)).where(AmlAlert.detected_at >= since)
        )).scalar_one()
    )
    flagged_total = int(
        (await session.execute(
            select(func.count(Transaction.id))
            .where(Transaction.booked_at >= since, Transaction.is_flagged.is_(True))
        )).scalar_one()
    )
    currency = (
        await session.execute(
            select(Transaction.currency).where(Transaction.booked_at >= since).limit(1)
        )
    ).scalar_one_or_none()

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "summary": {
            "countries": len(countries),
            "cities": len(cities),
            "corridors": len(corridors),
            "customers": int(
                (await session.execute(select(func.count(Customer.id)))).scalar_one()),
            "transactions": total_transactions,
            "total_value": round(total_value, 2),
            "currency": currency or "INR",
            "domestic_country": geo.DOMESTIC_COUNTRY,
            "cross_border_transactions": int(cross_border_transactions),
            "cross_border_value": round(cross_border_value, 2),
            "cross_border_share_pct": round(cross_border_value / total_value * 100, 2)
            if total_value else 0.0,
            "high_risk_transactions": int(high_risk_transactions),
            "high_risk_value": round(high_risk_value, 2),
            "high_risk_share_pct": round(high_risk_value / total_value * 100, 2)
            if total_value else 0.0,
            "flagged_transactions": flagged_total,
            "alerts": alerts,
        },
        "countries": countries,
        "cities": cities,
        "corridors": corridors[:40],
        "trend": trend,
        "unmapped": sorted(set(unmapped)),
    }
