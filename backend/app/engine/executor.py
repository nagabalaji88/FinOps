"""Execution engine: runs the agent graph, persists the trace, handles suspend/resume."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.bus import AGENT_CHANNEL, bus
from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.logging import execution_id_ctx, get_logger, trace_id_ctx
from app.core.metrics import (
    active_executions,
    agent_execution_duration,
    agent_executions_total,
    queue_depth,
)
from app.core.otel import get_tracer
from app.core.redaction import client_safe_error
from app.core.resilience import Bulkhead
from app.db.models.agents import Agent, Execution, Span
from app.db.session import ambient_session, session_scope
from app.engine.events import EventEmitter, EventType
from app.engine.nodes import DEFAULT_NODES, ApprovalPause, RunContext
from app.engine.state import Budget, ExecutionState

log = get_logger("engine")


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalise everything to UTC-aware."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


tracer = get_tracer("finops.engine")

bulkhead = Bulkhead("executions", settings.max_concurrent_executions)
_running: dict[str, asyncio.Task] = {}


class ExecutionEngine:
    async def submit(
        self,
        session: AsyncSession,
        *,
        agent_key: str,
        payload: dict[str, Any],
        user: Any | None = None,
        trigger: str = "manual",
        thread_id: str | None = None,
        request_id: str | None = None,
        wait: bool = False,
    ) -> Execution:
        from app.agents.registry import agent_registry

        agent_row = (await session.execute(select(Agent).where(Agent.key == agent_key))).scalar_one_or_none()
        if agent_row is None:
            raise NotFoundError(f"Agent '{agent_key}' is not registered")
        if agent_row.availability != "implemented":
            raise ValidationError(
                f"Agent '{agent_key}' is not implemented yet",
                details={"availability": agent_row.availability},
            )
        if agent_row.lifecycle_state != "active":
            raise ValidationError(
                f"Agent '{agent_key}' is {agent_row.lifecycle_state}",
                details={"lifecycle_state": agent_row.lifecycle_state},
            )
        spec = agent_registry.get(agent_key)
        spec.validate_input(payload)

        # Fail before spending. Every node past the planner needs a model, so queueing a run
        # with no credential anywhere buys a row, a trace and a wall of retries to arrive at
        # a message about circuit state -- which names neither the cause nor the fix. A
        # circuit-open provider is *not* refused here: breakers half-open on their own, so
        # that run may well succeed by the time it reaches the planner.
        from app.llm.router import router as model_router

        readiness = model_router.readiness()
        if not readiness["configured"]:
            raise ValidationError(
                "No LLM provider is configured, so this agent cannot run",
                details={
                    "set_one_of": readiness["set_one_of"],
                    "hint": "Put one in backend/.env, then restart the API",
                },
            )

        execution = Execution(
            agent_key=agent_key,
            agent_version=agent_row.version,
            status="queued",
            trigger=trigger,
            input=payload,
            trace_id=uuid.uuid4().hex,
            correlation_id=uuid.uuid4().hex[:16],
            request_id=request_id,
            thread_id=thread_id,
            user_id=getattr(user, "id", None),
            user_email=getattr(user, "email", None),
            department=getattr(user, "department", None),
            model=(agent_row.config or {}).get("model") or spec.model,
        )
        session.add(execution)
        await session.flush()
        await session.commit()

        queue_depth.labels(queue="executions").set(
            queue_depth.labels(queue="executions")._value.get() + 1  # type: ignore[attr-defined]
        )
        await bus.publish(
            AGENT_CHANNEL,
            {
                "type": "execution.queued",
                "execution_id": execution.id,
                "agent_key": agent_key,
                "status": "queued",
                "created_at": execution.created_at.isoformat(),
            },
        )

        execution_id = execution.id
        task = asyncio.create_task(self._run_detached(execution_id))
        _running[execution_id] = task
        task.add_done_callback(lambda _t: _running.pop(execution_id, None))
        if wait:
            await asyncio.wait_for(task, timeout=settings.execution_timeout_seconds + 30)
            await session.refresh(execution)
        return execution

    async def _run_detached(self, execution_id: str) -> None:
        try:
            async with session_scope() as session:
                await self.run(session, execution_id)
        except Exception as exc:  # pragma: no cover - top level guard
            log.exception("execution_task_crashed", execution_id=execution_id, error=str(exc))

    async def cancel(self, execution_id: str) -> bool:
        task = _running.get(execution_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def run(self, session: AsyncSession, execution_id: str, *, resume: bool = False) -> Execution:
        from app.agents.registry import agent_registry

        execution = (
            await session.execute(select(Execution).where(Execution.id == execution_id))
        ).scalar_one_or_none()
        if execution is None:
            raise NotFoundError(f"Execution '{execution_id}' not found")

        spec = agent_registry.get(execution.agent_key)
        execution_id_ctx.set(execution.id)
        trace_id_ctx.set(execution.trace_id)

        state = ExecutionState(
            execution_id=execution.id,
            agent_key=execution.agent_key,
            trace_id=execution.trace_id,
            correlation_id=execution.correlation_id,
            input=execution.input or {},
            config=spec.to_config(),
            user_id=execution.user_id,
            user_email=execution.user_email,
            department=execution.department,
            thread_id=execution.thread_id,
            model=execution.model or spec.model,
            budget=Budget(max_cost_usd=min(spec.cost_cap_usd, settings.per_execution_cost_cap_usd)),
        )
        if resume and execution.checkpoint:
            state.restore(execution.checkpoint)

        emitter = EventEmitter(session, state, execution.agent_key)
        ctx = RunContext(session=session, state=state, emitter=emitter, agent=spec)
        ambient_token = ambient_session.set(session)

        queued_at = _aware(execution.created_at) or datetime.now(UTC)
        execution.status = "running"
        execution.started_at = _aware(execution.started_at) or datetime.now(UTC)
        if not resume:
            execution.queue_ms = int((execution.started_at - queued_at).total_seconds() * 1000)
        await session.flush()

        root_span = Span(
            execution_id=execution.id,
            trace_id=execution.trace_id,
            span_id=state.root_span_id or state.new_span_id(),
            parent_span_id=None,
            name=f"agent.{execution.agent_key}",
            kind="agent",
            status="running",
            start_time=datetime.now(UTC),
            input_payload=execution.input,
            request_id=execution.request_id,
            attributes={
                "agent": execution.agent_key,
                "trigger": execution.trigger,
                "resumed": resume,
                "user": execution.user_email,
            },
        )
        session.add(root_span)
        state.root_span_id = root_span.span_id
        state.span_stack = [root_span.span_id]
        await session.flush()

        await emitter.emit(
            EventType.EXECUTION_STARTED if not resume else "execution.resumed",
            {
                "agent_key": execution.agent_key,
                "input": execution.input,
                "model": state.model,
                "resumed": resume,
                "graph": [n.key for n in DEFAULT_NODES],
            },
            node=None,
            log_message=f"Execution {'resumed' if resume else 'started'} for {execution.agent_key}",
        )
        await session.commit()

        started_perf = time.perf_counter()
        active_executions.inc()
        status = "succeeded"
        error_message: str | None = None
        error_type: str | None = None

        try:
            async with bulkhead:
                with tracer.start_as_current_span(f"agent.{execution.agent_key}") as otel_span:
                    otel_span.set_attribute("finops.execution_id", execution.id)
                    otel_span.set_attribute("finops.agent", execution.agent_key)
                    await asyncio.wait_for(
                        self._run_nodes(ctx, resume=resume),
                        timeout=settings.execution_timeout_seconds,
                    )
        except ApprovalPause as pause:
            status = "awaiting_approval"
            execution.checkpoint = state.to_checkpoint()
            flag_modified(execution, "checkpoint")
            execution.status = status
            await self._finalise(session, execution, state, root_span, status, None, None, started_perf)
            await emitter.emit(
                "execution.suspended",
                {
                    "approval_id": pause.approval.approval_id,
                    "node": pause.approval.node,
                    "title": pause.approval.title,
                },
                log_message=f"Execution suspended awaiting approval {pause.approval.approval_id}",
            )
            await session.commit()
            active_executions.dec()
            return execution
        except asyncio.CancelledError:
            status = "cancelled"
            error_message = "Execution cancelled"
            error_type = "CancelledError"
            await emitter.emit(EventType.EXECUTION_CANCELLED, {}, log_message="Execution cancelled")
        except TimeoutError:
            status = "timeout"
            error_message = f"Execution exceeded {settings.execution_timeout_seconds}s"
            error_type = "TimeoutError"
        except AppError as exc:
            status = "failed"
            error_message = exc.message
            error_type = type(exc).__name__
        except Exception as exc:
            status = "failed"
            # This message is persisted on the execution and served by the API, so it leaves
            # the server. The full text, frames and all, is in the log line below.
            error_message = client_safe_error(str(exc), reveal_internals=settings.debug)
            error_type = type(exc).__name__
            log.exception("execution_failed", execution_id=execution.id, error=str(exc))
        finally:
            active_executions.dec()
            ambient_session.reset(ambient_token)

        await self._finalise(
            session, execution, state, root_span, status, error_message, error_type, started_perf
        )
        if status == "succeeded":
            await emitter.emit(
                EventType.EXECUTION_COMPLETED,
                {
                    "latency_ms": execution.latency_ms,
                    "cost_usd": round(state.cost_usd, 6),
                    "tokens": state.tokens_input + state.tokens_output,
                    "response": state.final_response,
                },
                log_message=f"Execution completed in {execution.latency_ms}ms (${state.cost_usd:.4f})",
            )
        else:
            await emitter.emit(
                EventType.EXECUTION_FAILED,
                {"error": error_message, "error_type": error_type, "status": status},
                log_message=f"Execution {status}: {error_message}",
            )
        await session.commit()
        return execution

    async def _run_nodes(self, ctx: RunContext, *, resume: bool) -> None:
        nodes = DEFAULT_NODES
        start_index = 0
        if resume and ctx.state.current_node:
            keys = [n.key for n in nodes]
            # 'tools' is executed inside the reasoning node, so resume there.
            resume_key = "llm" if ctx.state.current_node == "tools" else ctx.state.current_node
            if resume_key in keys:
                start_index = keys.index(resume_key)
        for node in nodes[start_index:]:
            await node(ctx)
            # Commit per node: the trace, events and logs become visible to readers as the
            # execution progresses, and write transactions stay short.
            await ctx.session.commit()

    async def _finalise(
        self,
        session: AsyncSession,
        execution: Execution,
        state: ExecutionState,
        root_span: Span,
        status: str,
        error: str | None,
        error_type: str | None,
        started_perf: float,
    ) -> None:
        now = datetime.now(UTC)
        latency_ms = int((time.perf_counter() - started_perf) * 1000)
        execution.status = status
        execution.error = error
        execution.error_type = error_type
        execution.final_response = state.final_response
        execution.output = state.output or None
        execution.plan = state.plan
        execution.reasoning = "\n\n".join(state.reasoning_log)[:20000] or None
        execution.tokens_input = state.tokens_input
        execution.tokens_output = state.tokens_output
        execution.tokens_cached = state.tokens_cached
        execution.tokens_embedding = state.tokens_embedding
        execution.cost_usd = round(state.cost_usd, 6)
        execution.model = state.model
        execution.provider = state.provider
        execution.retry_count = state.retry_count
        execution.tool_call_count = state.tool_calls
        execution.llm_call_count = state.llm_calls
        execution.approval_count = state.approval_count
        execution.node_path = state.node_path
        execution.artifacts = state.artifacts
        if status != "awaiting_approval":
            execution.finished_at = now
            execution.latency_ms = latency_ms
            execution.checkpoint = None
            agent_executions_total.labels(execution.agent_key, status).inc()
            agent_execution_duration.labels(execution.agent_key).observe(latency_ms / 1000)

        root_span.end_time = now
        root_span.duration_ms = latency_ms
        root_span.status = {"succeeded": "ok", "awaiting_approval": "running"}.get(status, "error")
        root_span.error = error
        root_span.output_payload = {"response": (state.final_response or "")[:4000], "status": status}
        root_span.tokens_input = state.tokens_input
        root_span.tokens_output = state.tokens_output
        root_span.cost_usd = round(state.cost_usd, 6)
        root_span.attributes = {
            **(root_span.attributes or {}),
            "node_path": state.node_path,
            "llm_calls": state.llm_calls,
            "tool_calls": state.tool_calls,
        }
        current = queue_depth.labels(queue="executions")._value.get()  # type: ignore[attr-defined]
        queue_depth.labels(queue="executions").set(max(current - 1, 0))
        await session.flush()
        await bus.publish(
            AGENT_CHANNEL,
            {
                "type": "execution.updated",
                "execution_id": execution.id,
                "agent_key": execution.agent_key,
                "status": status,
                "latency_ms": execution.latency_ms,
                "cost_usd": execution.cost_usd,
            },
        )

    async def resume_after_approval(
        self, session: AsyncSession, execution_id: str, *, approved: bool, approval_payload: dict
    ) -> Execution:
        execution = (
            await session.execute(select(Execution).where(Execution.id == execution_id))
        ).scalar_one_or_none()
        if execution is None:
            raise NotFoundError(f"Execution '{execution_id}' not found")
        if execution.status != "awaiting_approval":
            raise ValidationError(f"Execution is {execution.status}, not awaiting approval")

        checkpoint = copy.deepcopy(execution.checkpoint or {})
        if approval_payload.get("final"):
            checkpoint.setdefault("scratch", {})["final_approval_granted"] = approved
            if not approved:
                execution.status = "cancelled"
                execution.error = "Rejected by reviewer"
                execution.finished_at = datetime.now(UTC)
                execution.checkpoint = None
                agent_executions_total.labels(execution.agent_key, "rejected").inc()
                await session.flush()
                return execution
            checkpoint["current_node"] = "response"
        else:
            tool_call_id = approval_payload.get("tool_call_id")
            if tool_call_id:
                checkpoint.setdefault("approved_tool_calls", {})[tool_call_id] = approved
            checkpoint["current_node"] = "llm"
            # Re-enter the reasoning loop and replay the pending tool calls.
            checkpoint["iteration"] = max(int(checkpoint.get("iteration", 1)) - 1, 0)
        execution.checkpoint = checkpoint
        flag_modified(execution, "checkpoint")
        execution.status = "running"
        await session.flush()
        await session.commit()

        task = asyncio.create_task(self._resume_detached(execution_id))
        _running[execution_id] = task
        task.add_done_callback(lambda _t: _running.pop(execution_id, None))
        return execution

    async def _resume_detached(self, execution_id: str) -> None:
        try:
            async with session_scope() as session:
                await self.run(session, execution_id, resume=True)
        except Exception as exc:  # pragma: no cover
            log.exception("resume_failed", execution_id=execution_id, error=str(exc))


engine = ExecutionEngine()
