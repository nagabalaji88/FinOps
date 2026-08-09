"""Execution event emission: persisted for replay, published for live streaming."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bus import bus, execution_channel
from app.db.models.agents import ExecutionEvent, LogRecord
from app.engine.state import ExecutionState


class EventType(StrEnum):
    EXECUTION_QUEUED = "execution.queued"
    EXECUTION_STARTED = "execution.started"
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_SKIPPED = "node.skipped"
    NODE_FAILED = "node.failed"
    PLANNING = "planning"
    REASONING = "reasoning"
    KNOWLEDGE_SEARCH = "knowledge.search"
    VECTOR_SEARCH = "vector.search"
    MEMORY_RETRIEVAL = "memory.retrieval"
    DATABASE_QUERY = "database.query"
    API_CALL = "api.call"
    LLM_STARTED = "llm.started"
    LLM_DELTA = "llm.delta"
    LLM_COMPLETED = "llm.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    RETRY = "retry"
    GUARDRAIL = "guardrail"
    VALIDATION = "validation"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    ARTIFACT = "artifact"
    COST = "cost"
    FINAL_RESPONSE = "final.response"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_CANCELLED = "execution.cancelled"


LEVEL_FOR_EVENT = {
    EventType.NODE_FAILED: "ERROR",
    EventType.TOOL_FAILED: "ERROR",
    EventType.EXECUTION_FAILED: "ERROR",
    EventType.RETRY: "WARNING",
    EventType.GUARDRAIL: "WARNING",
    EventType.APPROVAL_REQUESTED: "WARNING",
}


class EventEmitter:
    """Writes to the event log, the log store and the live bus."""

    def __init__(self, session: AsyncSession, state: ExecutionState, agent_key: str):
        self.session = session
        self.state = state
        self.agent_key = agent_key

    async def emit(
        self,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        *,
        node: str | None = None,
        span_id: str | None = None,
        persist: bool = True,
        log_message: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        payload = payload or {}
        sequence = self.state.next_sequence()
        event = {
            "execution_id": self.state.execution_id,
            "agent_key": self.agent_key,
            "sequence": sequence,
            "type": str(event_type),
            "node": node or self.state.current_node,
            "span_id": span_id or self.state.parent_span(),
            "trace_id": self.state.trace_id,
            "correlation_id": self.state.correlation_id,
            "timestamp": now.isoformat(),
            "payload": payload,
        }
        if persist:
            self.session.add(
                ExecutionEvent(
                    execution_id=self.state.execution_id,
                    sequence=sequence,
                    type=str(event_type),
                    node=event["node"],
                    span_id=event["span_id"],
                    payload=payload,
                    timestamp=now,
                )
            )
        if log_message:
            level = LEVEL_FOR_EVENT.get(event_type, "INFO")  # type: ignore[arg-type]
            self.session.add(
                LogRecord(
                    timestamp=now,
                    level=level,
                    logger=f"agent.{self.agent_key}",
                    message=log_message,
                    agent_key=self.agent_key,
                    execution_id=self.state.execution_id,
                    correlation_id=self.state.correlation_id,
                    trace_id=self.state.trace_id,
                    span_id=event["span_id"],
                    user_email=self.state.user_email,
                    attributes={"event": str(event_type), "node": event["node"], **payload},
                )
            )
        await bus.publish(execution_channel(self.state.execution_id), event)
        await bus.publish(
            "executions:stream",
            {k: v for k, v in event.items() if k != "payload"} | {"payload_keys": list(payload)},
        )
        return event
