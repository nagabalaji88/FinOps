"""Agent registry, versions, executions, spans, logs, approvals, cost and evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class Agent(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "agents"

    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="General")
    # implemented | coming_soon
    availability: Mapped[str] = mapped_column(String(24), default="implemented", index=True)
    # active | paused | disabled
    lifecycle_state: Mapped[str] = mapped_column(String(24), default="active", index=True)
    owner: Mapped[str] = mapped_column(String(160), default="Platform Engineering")
    owner_email: Mapped[str | None] = mapped_column(String(255), default=None)
    department: Mapped[str] = mapped_column(String(120), default="Technology")
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    tools: Mapped[list[str]] = mapped_column(JSONType, default=list)
    knowledge_sources: Mapped[list[str]] = mapped_column(JSONType, default=list)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
    sla_latency_ms: Mapped[int] = mapped_column(Integer, default=30000)
    monthly_budget_usd: Mapped[float] = mapped_column(Float, default=1000.0)

    versions: Mapped[list[AgentVersion]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", order_by="AgentVersion.version.desc()"
    )


class AgentVersion(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "agent_versions"
    __table_args__ = (Index("ix_agent_version_unique", "agent_id", "version", unique=True),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    changelog: Mapped[str] = mapped_column(Text, default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    published_by: Mapped[str | None] = mapped_column(String(255), default=None)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    agent: Mapped[Agent] = relationship(back_populates="versions")


class Execution(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_exec_agent_created", "agent_key", "created_at"),
        Index("ix_exec_status_created", "status", "created_at"),
    )

    agent_key: Mapped[str] = mapped_column(String(80), index=True)
    agent_version: Mapped[int] = mapped_column(Integer, default=1)
    # queued | running | awaiting_approval | succeeded | failed | cancelled | timeout
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")  # manual|schedule|api|webhook
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    final_response: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    error_type: Mapped[str | None] = mapped_column(String(80), default=None)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    reasoning: Mapped[str | None] = mapped_column(Text, default=None)

    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    user_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    user_email: Mapped[str | None] = mapped_column(String(255), default=None)
    department: Mapped[str | None] = mapped_column(String(120), index=True, default=None)

    model: Mapped[str | None] = mapped_column(String(120), default=None)
    provider: Mapped[str | None] = mapped_column(String(60), default=None)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    tokens_embedding: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    queue_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_call_count: Mapped[int] = mapped_column(Integer, default=0)
    approval_count: Mapped[int] = mapped_column(Integer, default=0)
    node_path: Mapped[list[str]] = mapped_column(JSONType, default=list)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class Span(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "spans"
    __table_args__ = (Index("ix_span_exec_start", "execution_id", "start_time"),)

    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    span_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    name: Mapped[str] = mapped_column(String(200))
    # planner|retriever|memory|llm|tool|validation|guardrail|approval|response|db|http|vector
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running|ok|error|skipped
    start_time: Mapped[datetime] = mapped_column(UTCDateTime)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float, default=None)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)


class ExecutionEvent(Base, UUIDMixin):
    """Append-only stream of execution events (replayable for late subscribers)."""

    __tablename__ = "execution_events"
    __table_args__ = (Index("ix_event_exec_seq", "execution_id", "sequence"),)

    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(60), index=True)
    node: Mapped[str | None] = mapped_column(String(60), default=None)
    span_id: Mapped[str | None] = mapped_column(String(64), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class LogRecord(Base, UUIDMixin):
    __tablename__ = "log_records"
    __table_args__ = (
        Index("ix_log_ts_level", "timestamp", "level"),
        Index("ix_log_exec", "execution_id", "timestamp"),
    )

    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    logger: Mapped[str] = mapped_column(String(120), default="finops")
    message: Mapped[str] = mapped_column(Text)
    agent_key: Mapped[str | None] = mapped_column(String(80), index=True, default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    span_id: Mapped[str | None] = mapped_column(String(64), default=None)
    user_email: Mapped[str | None] = mapped_column(String(255), default=None)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Approval(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "approvals"

    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(80), index=True)
    node: Mapped[str] = mapped_column(String(60), default="human_approval")
    title: Mapped[str] = mapped_column(String(240))
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    risk_level: Mapped[str] = mapped_column(String(16), default="medium")  # low|medium|high|critical
    required_role: Mapped[str] = mapped_column(String(60), default="approver")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), default=None)
    reviewer_id: Mapped[str | None] = mapped_column(String(36), default=None)
    reviewer_email: Mapped[str | None] = mapped_column(String(255), default=None)
    comments: Mapped[str | None] = mapped_column(Text, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)


class CostRecord(Base, UUIDMixin):
    __tablename__ = "cost_records"
    __table_args__ = (
        Index("ix_cost_ts_agent", "timestamp", "agent_key"),
        Index("ix_cost_model", "model"),
    )

    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    span_id: Mapped[str | None] = mapped_column(String(64), default=None)
    agent_key: Mapped[str | None] = mapped_column(String(80), index=True, default=None)
    category: Mapped[str] = mapped_column(String(32), default="llm")  # llm|embedding|tool|api|storage
    provider: Mapped[str] = mapped_column(String(60), index=True)
    model: Mapped[str | None] = mapped_column(String(120), default=None)
    tool: Mapped[str | None] = mapped_column(String(120), default=None)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    user_email: Mapped[str | None] = mapped_column(String(255), default=None)
    department: Mapped[str | None] = mapped_column(String(120), index=True, default=None)
    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)


class Evaluation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "evaluations"

    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_key: Mapped[str] = mapped_column(String(80), index=True)
    evaluator: Mapped[str] = mapped_column(String(60), default="automatic")  # automatic|human|llm_judge
    faithfulness: Mapped[float | None] = mapped_column(Float, default=None)
    groundedness: Mapped[float | None] = mapped_column(Float, default=None)
    hallucination_score: Mapped[float | None] = mapped_column(Float, default=None)
    citation_score: Mapped[float | None] = mapped_column(Float, default=None)
    tool_success_rate: Mapped[float | None] = mapped_column(Float, default=None)
    answer_relevance: Mapped[float | None] = mapped_column(Float, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    human_rating: Mapped[int | None] = mapped_column(Integer, default=None)
    human_feedback: Mapped[str | None] = mapped_column(Text, default=None)
    reviewer_email: Mapped[str | None] = mapped_column(String(255), default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PlaygroundRun(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "playground_runs"

    prompt: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text, default=None)
    models: Mapped[list[str]] = mapped_column(JSONType, default=list)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    user_email: Mapped[str | None] = mapped_column(String(255), default=None)


class ToolHealth(Base, UUIDMixin, TimestampMixin):
    """Rolling health snapshot per tool/integration, updated on every invocation."""

    __tablename__ = "tool_health"

    tool_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(60), default="internal")
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_timeouts: Mapped[int] = mapped_column(Integer, default=0)
    total_retries: Mapped[int] = mapped_column(Integer, default=0)
    p50_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    p95_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    last_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    last_status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_called_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    latency_samples: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)


class ScheduledJob(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "scheduled_jobs"

    agent_key: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
