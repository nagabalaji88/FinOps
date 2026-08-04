"""Executions: listing, detail, OpenTelemetry-style traces, events, logs and live streaming."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.bus import bus, execution_channel
from app.core.errors import NotFoundError
from app.core.rbac import Permission
from app.db.models.agents import Execution, ExecutionEvent, LogRecord, Span
from app.db.session import session_scope
from app.engine.executor import engine
from app.engine.nodes import GRAPH_DEFINITION, GRAPH_EDGES

router = APIRouter(prefix="/executions", tags=["executions"])


def _serialise_execution(e: Execution, *, full: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": e.id,
        "agent_key": e.agent_key,
        "agent_version": e.agent_version,
        "status": e.status,
        "trigger": e.trigger,
        "trace_id": e.trace_id,
        "correlation_id": e.correlation_id,
        "thread_id": e.thread_id,
        "user_email": e.user_email,
        "department": e.department,
        "model": e.model,
        "provider": e.provider,
        "tokens": {
            "input": e.tokens_input, "output": e.tokens_output,
            "cached": e.tokens_cached, "embedding": e.tokens_embedding,
            "total": e.tokens_input + e.tokens_output,
        },
        "cost_usd": round(e.cost_usd, 6),
        "latency_ms": e.latency_ms,
        "queue_ms": e.queue_ms,
        "retry_count": e.retry_count,
        "tool_call_count": e.tool_call_count,
        "llm_call_count": e.llm_call_count,
        "approval_count": e.approval_count,
        "node_path": e.node_path or [],
        "error": e.error,
        "error_type": e.error_type,
        "created_at": e.created_at.isoformat(),
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "finished_at": e.finished_at.isoformat() if e.finished_at else None,
    }
    if full:
        payload.update({
            "input": e.input,
            "output": e.output,
            "final_response": e.final_response,
            "plan": e.plan,
            "reasoning": e.reasoning,
            "artifacts": e.artifacts or [],
            "metadata": e.metadata_ or {},
        })
    return payload


@router.get("")
async def list_executions(
    session: SessionDep,
    principal: PrincipalDep,
    agent_key: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    user_email: str | None = None,
    since_hours: int = Query(default=168, ge=1, le=8760),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    principal.require(Permission.EXECUTION_READ)
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    stmt = select(Execution).where(Execution.created_at >= since)
    count_stmt = select(func.count(Execution.id)).where(Execution.created_at >= since)
    if agent_key:
        stmt = stmt.where(Execution.agent_key == agent_key)
        count_stmt = count_stmt.where(Execution.agent_key == agent_key)
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",")]
        stmt = stmt.where(Execution.status.in_(statuses))
        count_stmt = count_stmt.where(Execution.status.in_(statuses))
    if user_email:
        stmt = stmt.where(Execution.user_email == user_email)
        count_stmt = count_stmt.where(Execution.user_email == user_email)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(Execution.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_serialise_execution(e) for e in rows]}


@router.get("/{execution_id}")
async def get_execution(execution_id: str, session: SessionDep,
                        principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.EXECUTION_READ)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    return _serialise_execution(execution, full=True)


@router.get("/{execution_id}/trace")
async def get_trace(execution_id: str, session: SessionDep,
                    principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.TRACE_READ)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    spans = (
        await session.execute(
            select(Span).where(Span.execution_id == execution_id).order_by(Span.start_time)
        )
    ).scalars().all()
    if not spans:
        return {"trace_id": execution.trace_id, "spans": [], "root_span_id": None}

    trace_start = min(s.start_time for s in spans)
    trace_end = max((s.end_time or s.start_time) for s in spans)
    total_ms = max((trace_end - trace_start).total_seconds() * 1000, 0.001)

    serialised = []
    for span in spans:
        offset_ms = (span.start_time - trace_start).total_seconds() * 1000
        duration = span.duration_ms or 0.0
        serialised.append({
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "trace_id": span.trace_id,
            "name": span.name,
            "kind": span.kind,
            "status": span.status,
            "start_time": span.start_time.isoformat(),
            "end_time": span.end_time.isoformat() if span.end_time else None,
            "duration_ms": round(duration, 3),
            "offset_ms": round(offset_ms, 3),
            "offset_pct": round(offset_ms / total_ms * 100, 3),
            "width_pct": round(duration / total_ms * 100, 3),
            "attributes": span.attributes or {},
            "events": span.events or [],
            "input": span.input_payload,
            "output": span.output_payload,
            "error": span.error,
            "retry_count": span.retry_count,
            "tokens": {"input": span.tokens_input, "output": span.tokens_output},
            "cost_usd": round(span.cost_usd, 6),
            "request_id": span.request_id,
        })
    return {
        "trace_id": execution.trace_id,
        "execution_id": execution_id,
        "agent_key": execution.agent_key,
        "root_span_id": next((s["span_id"] for s in serialised if not s["parent_span_id"]), None),
        "start_time": trace_start.isoformat(),
        "end_time": trace_end.isoformat(),
        "total_duration_ms": round(total_ms, 3),
        "span_count": len(serialised),
        "error_count": sum(1 for s in serialised if s["status"] == "error"),
        "total_cost_usd": round(sum(s["cost_usd"] for s in serialised), 6),
        "spans": serialised,
    }


@router.get("/{execution_id}/graph")
async def get_execution_graph(execution_id: str, session: SessionDep,
                              principal: PrincipalDep) -> dict[str, Any]:
    """React Flow DAG annotated with what actually happened in this execution."""
    principal.require(Permission.EXECUTION_READ)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    spans = (
        await session.execute(select(Span).where(Span.execution_id == execution_id))
    ).scalars().all()

    by_node: dict[str, list[Span]] = {}
    for span in spans:
        node = (span.attributes or {}).get("node") or span.kind
        by_node.setdefault(node, []).append(span)

    kind_to_node = {
        "planner": "planner", "retriever": "retriever", "memory": "memory", "llm": "llm",
        "tool": "tools", "validation": "validation", "guardrail": "guardrails",
        "approval": "human_approval", "response": "response",
    }
    for span in spans:
        node = kind_to_node.get(span.kind)
        if node:
            by_node.setdefault(node, []).append(span)

    executed = set(execution.node_path or [])
    nodes = []
    for definition in GRAPH_DEFINITION:
        node_spans = [s for s in by_node.get(definition["id"], [])]
        duration = sum(s.duration_ms or 0 for s in node_spans)
        errors = [s for s in node_spans if s.status == "error"]
        state = "error" if errors else ("executed" if definition["id"] in executed
                                        or node_spans else "skipped")
        if execution.status in {"running", "awaiting_approval"} and \
                execution.node_path and execution.node_path[-1] == definition["id"]:
            state = "active"
        nodes.append({
            **definition,
            "state": state,
            "span_count": len(node_spans),
            "duration_ms": round(duration, 2),
            "cost_usd": round(sum(s.cost_usd for s in node_spans), 6),
            "tokens": sum(s.tokens_input + s.tokens_output for s in node_spans),
            "retries": sum(s.retry_count for s in node_spans),
            "spans": [
                {"span_id": s.span_id, "name": s.name, "status": s.status,
                 "duration_ms": round(s.duration_ms or 0, 2), "error": s.error}
                for s in node_spans
            ],
        })
    return {"execution_id": execution_id, "status": execution.status, "nodes": nodes,
            "edges": GRAPH_EDGES}


@router.get("/{execution_id}/events")
async def get_events(execution_id: str, session: SessionDep, principal: PrincipalDep,
                     after: int = Query(default=0, ge=0)) -> list[dict[str, Any]]:
    principal.require(Permission.EXECUTION_READ)
    events = (
        await session.execute(
            select(ExecutionEvent).where(
                ExecutionEvent.execution_id == execution_id, ExecutionEvent.sequence > after
            ).order_by(ExecutionEvent.sequence)
        )
    ).scalars().all()
    return [
        {"sequence": e.sequence, "type": e.type, "node": e.node, "span_id": e.span_id,
         "timestamp": e.timestamp.isoformat(), "payload": e.payload}
        for e in events
    ]


@router.get("/{execution_id}/logs")
async def get_execution_logs(execution_id: str, session: SessionDep,
                             principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.LOG_READ)
    logs = (
        await session.execute(
            select(LogRecord).where(LogRecord.execution_id == execution_id)
            .order_by(LogRecord.timestamp)
        )
    ).scalars().all()
    return [
        {"timestamp": log_row.timestamp.isoformat(), "level": log_row.level,
         "logger": log_row.logger, "message": log_row.message,
         "correlation_id": log_row.correlation_id, "trace_id": log_row.trace_id,
         "span_id": log_row.span_id, "attributes": log_row.attributes}
        for log_row in logs
    ]


@router.post("/{execution_id}/cancel")
async def cancel_execution(execution_id: str, request: Request, session: SessionDep,
                           principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.EXECUTION_CANCEL)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    cancelled = await engine.cancel(execution_id)
    if not cancelled and execution.status in {"queued", "running", "awaiting_approval"}:
        execution.status = "cancelled"
        execution.finished_at = datetime.now(UTC)
        execution.error = "Cancelled by operator"
    await write_audit(session, principal=principal, action="execution.cancel",
                      resource_type="execution", resource_id=execution_id, severity="warning",
                      request=request)
    return {"execution_id": execution_id, "cancelled": True, "task_cancelled": cancelled}


@router.get("/{execution_id}/stream")
async def stream_execution(execution_id: str, request: Request, session: SessionDep,
                           principal: PrincipalDep, after: int = Query(default=0, ge=0)):
    """Server-Sent Events stream: replays persisted events, then follows live ones."""
    principal.require(Permission.EXECUTION_READ)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    channel = execution_channel(execution_id)
    terminal = {"execution.completed", "execution.failed", "execution.cancelled",
                "execution.suspended"}

    async def generator():
        async with bus.subscribe(channel) as queue:
            async with session_scope() as replay_session:
                events = (
                    await replay_session.execute(
                        select(ExecutionEvent).where(
                            ExecutionEvent.execution_id == execution_id,
                            ExecutionEvent.sequence > after,
                        ).order_by(ExecutionEvent.sequence)
                    )
                ).scalars().all()
                last_sequence = after
                for event in events:
                    last_sequence = event.sequence
                    yield {
                        "event": event.type,
                        "id": str(event.sequence),
                        "data": json.dumps({
                            "sequence": event.sequence, "type": event.type, "node": event.node,
                            "span_id": event.span_id, "timestamp": event.timestamp.isoformat(),
                            "payload": event.payload,
                        }),
                    }
                if events and events[-1].type in terminal:
                    return

            while True:
                if await request.is_disconnected():
                    return
                try:
                    _, event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"event": "heartbeat", "data": json.dumps(
                        {"timestamp": datetime.now(UTC).isoformat()})}
                    continue
                if event.get("sequence", 0) <= last_sequence:
                    continue
                last_sequence = event.get("sequence", last_sequence)
                yield {"event": event["type"], "id": str(event.get("sequence", "")),
                       "data": json.dumps(event)}
                if event["type"] in terminal:
                    return

    return EventSourceResponse(generator(), ping=15)


@router.websocket("/{execution_id}/ws")
async def execution_websocket(websocket: WebSocket, execution_id: str) -> None:
    """WebSocket alternative to the SSE stream (same event payloads)."""
    await websocket.accept()
    channel = execution_channel(execution_id)
    terminal = {"execution.completed", "execution.failed", "execution.cancelled",
                "execution.suspended"}
    try:
        async with session_scope() as session:
            events = (
                await session.execute(
                    select(ExecutionEvent).where(ExecutionEvent.execution_id == execution_id)
                    .order_by(ExecutionEvent.sequence)
                )
            ).scalars().all()
            for event in events:
                await websocket.send_json({
                    "sequence": event.sequence, "type": event.type, "node": event.node,
                    "timestamp": event.timestamp.isoformat(), "payload": event.payload,
                })
        async with bus.subscribe(channel) as queue:
            while True:
                try:
                    _, event = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    await websocket.send_json({"type": "heartbeat"})
                    continue
                await websocket.send_json(event)
                if event.get("type") in terminal:
                    break
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.close(code=1011)
