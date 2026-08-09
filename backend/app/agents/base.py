"""Agent specification: the declarative contract the execution graph runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationError
from app.llm.types import Message


@dataclass
class AgentSpec:
    key: str
    name: str
    description: str
    category: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    knowledge_sources: list[str] = field(default_factory=list)

    model: str | None = None
    planner_model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    max_iterations: int = 8
    retrieval_top_k: int = 6

    memory_enabled: bool = False
    memory_window: int = 12

    require_citations: bool = False
    strict_validation: bool = False
    output_schema: list[str] = field(default_factory=list)

    mask_pii: bool = True
    pii_allowlist: list[str] = field(default_factory=list)
    blocked_terms: list[str] = field(default_factory=list)
    required_disclaimer: str | None = None

    final_approval_required: bool = False
    final_approval_risk_threshold: str = "high"  # low|medium|high|critical|always
    approval_role: str = "approver"

    cost_cap_usd: float = 2.0
    sla_latency_ms: int = 60_000
    owner: str = "Platform Engineering"
    owner_email: str | None = None
    department: str = "Technology"
    tags: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    example_input: dict[str, Any] = field(default_factory=dict)
    availability: str = "implemented"

    # --- behaviour ---------------------------------------------------------
    def validate_input(self, payload: dict[str, Any]) -> None:
        required = [k for k, v in self.input_schema.items() if v.get("required")]
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ValidationError(
                f"Missing required input for agent '{self.key}'",
                details={"missing": missing, "schema": self.input_schema},
            )

    def build_messages(self, state: Any) -> list[Message]:
        """Compose the system + user turn from state (retrieval, memory, plan)."""
        system_parts = [self.system_prompt.strip()]
        if state.memory_summary:
            system_parts.append(f"## Conversation memory\n{state.memory_summary}")
        if state.scratch.get("retrieval_context"):
            system_parts.append(
                "## Retrieved knowledge\nCite sources using the bracketed markers shown.\n\n"
                + state.scratch["retrieval_context"]
            )
        if state.plan:
            system_parts.append(f"## Approved plan\n{json.dumps(state.plan, indent=2)[:3000]}")
        if self.output_schema:
            system_parts.append(
                "## Required output\nReturn a JSON object containing at least these keys: "
                + ", ".join(self.output_schema)
            )
        user_content = self.render_user_message(state.input)
        return [
            Message(role="system", content="\n\n".join(system_parts)),
            Message(role="user", content=user_content),
        ]

    def render_user_message(self, payload: dict[str, Any]) -> str:
        query = payload.get("query") or payload.get("question") or payload.get("message")
        extras = {
            k: v
            for k, v in payload.items()
            if k not in {"query", "question", "message"} and v not in (None, "", [], {})
        }
        parts = []
        if query:
            parts.append(str(query))
        if extras:
            parts.append("Context:\n" + json.dumps(extras, indent=2, default=str)[:4000])
        return "\n\n".join(parts) or json.dumps(payload, default=str)[:4000]

    def to_config(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "planner_model": self.planner_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_iterations": self.max_iterations,
            "retrieval_top_k": self.retrieval_top_k,
            "memory_enabled": self.memory_enabled,
            "memory_window": self.memory_window,
            "require_citations": self.require_citations,
            "strict_validation": self.strict_validation,
            "output_schema": self.output_schema,
            "mask_pii": self.mask_pii,
            "pii_allowlist": self.pii_allowlist,
            "blocked_terms": self.blocked_terms,
            "required_disclaimer": self.required_disclaimer,
            "final_approval_required": self.final_approval_required,
            "final_approval_risk_threshold": self.final_approval_risk_threshold,
            "approval_role": self.approval_role,
            "cost_cap_usd": self.cost_cap_usd,
            "sla_latency_ms": self.sla_latency_ms,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "knowledge_sources": self.knowledge_sources,
            "input_schema": self.input_schema,
            "example_input": self.example_input,
        }

    @classmethod
    def from_config(cls, base: AgentSpec, config: dict[str, Any]) -> AgentSpec:
        import dataclasses

        allowed = {f.name for f in dataclasses.fields(cls)}
        overrides = {k: v for k, v in (config or {}).items() if k in allowed and v is not None}
        return dataclasses.replace(base, **overrides)
