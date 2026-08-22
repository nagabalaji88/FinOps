"""Provider-agnostic chat, tool-calling and streaming types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in self.tool_calls
            ]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out


@dataclass(slots=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: Usage
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    request_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """The reply hit the output ceiling and stops mid-thought.

        Nothing raises for this: it is a *successful* call that returned an unusable result,
        so the damage surfaces later as a parse failure, or worse as an answer that reads
        complete and is missing its conclusion. Callers that parse a reply must check it.
        """
        return self.finish_reason in {"length", "max_tokens", "MAX_TOKENS"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "provider": self.provider,
            "finish_reason": self.finish_reason,
            "truncated": self.truncated,
            "latency_ms": round(self.latency_ms, 2),
            "cost_usd": round(self.cost_usd, 6),
            "request_id": self.request_id,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
                "reasoning_tokens": self.usage.reasoning_tokens,
            },
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in self.tool_calls
            ],
        }


@dataclass(slots=True)
class StreamChunk:
    delta: str = ""
    tool_call: ToolCall | None = None
    done: bool = False
    response: LLMResponse | None = None


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    provider: str
    dimensions: int
    usage: Usage
    cost_usd: float = 0.0
    latency_ms: float = 0.0
