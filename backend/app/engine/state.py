"""Mutable state carried through an execution graph, with checkpointing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.llm.types import Message, ToolCall


@dataclass
class Budget:
    max_cost_usd: float
    max_tokens: int = 500_000
    spent_usd: float = 0.0
    tokens_used: int = 0

    def charge(self, cost: float, tokens: int) -> None:
        self.spent_usd += cost
        self.tokens_used += tokens

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.max_cost_usd or self.tokens_used >= self.max_tokens

    def remaining(self) -> float:
        return max(self.max_cost_usd - self.spent_usd, 0.0)


@dataclass
class PendingApproval:
    approval_id: str
    tool_call: dict[str, Any] | None
    node: str
    title: str
    summary: str
    risk: str


@dataclass
class ExecutionState:
    execution_id: str
    agent_key: str
    trace_id: str
    correlation_id: str
    input: dict[str, Any]
    config: dict[str, Any]

    user_id: str | None = None
    user_email: str | None = None
    department: str | None = None
    roles: list[str] = field(default_factory=list)
    thread_id: str | None = None

    messages: list[Message] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    reasoning_log: list[str] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    memory_summary: str | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    guardrail_findings: list[dict[str, Any]] = field(default_factory=list)
    validation_findings: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)

    final_response: str | None = None
    output: dict[str, Any] = field(default_factory=dict)

    node_path: list[str] = field(default_factory=list)
    current_node: str | None = None
    iteration: int = 0
    retry_count: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    approval_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_embedding: int = 0
    tokens_cached: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    provider: str | None = None

    budget: Budget = field(default_factory=lambda: Budget(max_cost_usd=5.0))
    pending_approval: PendingApproval | None = None
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    approved_tool_calls: dict[str, bool] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    span_stack: list[str] = field(default_factory=list)
    root_span_id: str | None = None
    sequence: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def new_span_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def parent_span(self) -> str | None:
        return self.span_stack[-1] if self.span_stack else None

    def record_usage(self, *, input_tokens: int, output_tokens: int, cached: int, cost: float) -> None:
        self.tokens_input += input_tokens
        self.tokens_output += output_tokens
        self.tokens_cached += cached
        self.cost_usd += cost
        self.budget.charge(cost, input_tokens + output_tokens)

    # --- checkpointing -----------------------------------------------------
    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "plan": self.plan,
            "reasoning_log": self.reasoning_log,
            "retrieved": self.retrieved,
            "citations": self.citations,
            "memory_summary": self.memory_summary,
            "tool_results": self.tool_results,
            "guardrail_findings": self.guardrail_findings,
            "validation_findings": self.validation_findings,
            "artifacts": self.artifacts,
            "scratch": self.scratch,
            "node_path": self.node_path,
            "current_node": self.current_node,
            "iteration": self.iteration,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "approval_count": self.approval_count,
            "tokens": {
                "input": self.tokens_input,
                "output": self.tokens_output,
                "cached": self.tokens_cached,
                "embedding": self.tokens_embedding,
            },
            "cost_usd": self.cost_usd,
            "model": self.model,
            "provider": self.provider,
            "budget": {
                "max_cost_usd": self.budget.max_cost_usd,
                "spent_usd": self.budget.spent_usd,
                "tokens_used": self.budget.tokens_used,
            },
            "pending_tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in self.pending_tool_calls
            ],
            "approved_tool_calls": self.approved_tool_calls,
            "root_span_id": self.root_span_id,
            "sequence": self.sequence,
        }

    def restore(self, checkpoint: dict[str, Any]) -> None:
        self.messages = [
            Message(
                role=m["role"],
                content=m.get("content") or "",
                tool_calls=[
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments") or {})
                    for tc in m.get("tool_calls", [])
                ],
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            )
            for m in checkpoint.get("messages", [])
        ]
        self.plan = checkpoint.get("plan")
        self.reasoning_log = checkpoint.get("reasoning_log", [])
        self.retrieved = checkpoint.get("retrieved", [])
        self.citations = checkpoint.get("citations", [])
        self.memory_summary = checkpoint.get("memory_summary")
        self.tool_results = checkpoint.get("tool_results", [])
        self.guardrail_findings = checkpoint.get("guardrail_findings", [])
        self.validation_findings = checkpoint.get("validation_findings", [])
        self.artifacts = checkpoint.get("artifacts", [])
        self.scratch = checkpoint.get("scratch", {})
        self.node_path = checkpoint.get("node_path", [])
        self.current_node = checkpoint.get("current_node")
        self.iteration = checkpoint.get("iteration", 0)
        self.llm_calls = checkpoint.get("llm_calls", 0)
        self.tool_calls = checkpoint.get("tool_calls", 0)
        self.approval_count = checkpoint.get("approval_count", 0)
        tokens = checkpoint.get("tokens", {})
        self.tokens_input = tokens.get("input", 0)
        self.tokens_output = tokens.get("output", 0)
        self.tokens_cached = tokens.get("cached", 0)
        self.tokens_embedding = tokens.get("embedding", 0)
        self.cost_usd = checkpoint.get("cost_usd", 0.0)
        self.model = checkpoint.get("model")
        self.provider = checkpoint.get("provider")
        budget = checkpoint.get("budget", {})
        self.budget = Budget(
            max_cost_usd=budget.get("max_cost_usd", 5.0),
            spent_usd=budget.get("spent_usd", 0.0),
            tokens_used=budget.get("tokens_used", 0),
        )
        self.pending_tool_calls = [
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments") or {})
            for tc in checkpoint.get("pending_tool_calls", [])
        ]
        self.approved_tool_calls = checkpoint.get("approved_tool_calls", {})
        self.root_span_id = checkpoint.get("root_span_id")
        self.sequence = checkpoint.get("sequence", 0)
