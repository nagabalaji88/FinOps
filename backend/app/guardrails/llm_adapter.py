"""Bridges NeMo Guardrails onto the platform's model router.

NeMo's LLM-backed rails (self-check input/output, fact checking) need a model. Rather
than give NeMo its own provider credentials and its own HTTP client, this adapter
implements NeMo's framework-agnostic ``LLMModel`` protocol on top of `model_router`, so
rail calls go through the same routing, circuit breakers, retries, fallback chain and cost
metering as every other model call the platform makes — and land in the same cost ledger.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.router import router as model_router
from app.llm.types import Message

log = get_logger("guardrails.llm")

# Rails are classifiers, not writers: deterministic and short.
RAIL_TEMPERATURE = 0.0
RAIL_MAX_TOKENS = 256


def _to_messages(prompt: Any) -> list[Message]:
    """NeMo passes either a plain string or its own ChatMessage list."""
    if isinstance(prompt, str):
        return [Message(role="user", content=prompt)]

    messages: list[Message] = []
    for item in prompt:
        role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else None)
        content = getattr(item, "content", None) or (
            item.get("content") if isinstance(item, dict) else None
        )
        role_value = getattr(role, "value", role) or "user"
        if role_value not in {"user", "assistant", "system", "tool"}:
            role_value = "user"
        if isinstance(content, list):  # multi-part content: keep the text parts
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        messages.append(Message(role=role_value, content=str(content or "")))
    return messages or [Message(role="user", content="")]


class RouterLLM:
    """NeMo ``LLMModel`` backed by the platform's model router."""

    def __init__(self, *, model: str | None = None, context: dict[str, Any] | None = None):
        # Empty means "let the router choose": rails must run on whatever the deployment
        # can actually reach, not on a model named in a default that may be unavailable.
        self._model = model or settings.guardrails_model or ""
        self._context = context or {}
        self._provider: str | None = None

    # --- NeMo LLMModel protocol ---------------------------------------------
    @property
    def model_name(self) -> str:
        return self._model or "router-selected"

    @property
    def provider_name(self) -> str | None:
        return self._provider

    @property
    def provider_url(self) -> str | None:
        return None

    async def generate_async(self, prompt: Any, *, stop: list[str] | None = None, **kwargs: Any):
        from nemoguardrails.types import LLMResponse, UsageInfo

        response = await model_router.chat(
            messages=_to_messages(prompt),
            model=self._model or None,
            temperature=float(kwargs.get("temperature", RAIL_TEMPERATURE)),
            max_tokens=int(kwargs.get("max_tokens", RAIL_MAX_TOKENS)),
            stop=stop,
            context={**self._context, "purpose": "guardrail"},
        )
        self._provider = response.provider
        return LLMResponse(
            content=response.content,
            model=response.model,
            finish_reason="stop",
            request_id=response.request_id,
            usage=UsageInfo(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total,
            ),
        )

    async def stream_async(  # type: ignore[override]
        self, prompt: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Rails are evaluated whole, so streaming yields the single completed response."""
        from nemoguardrails.types import LLMResponseChunk

        response = await self.generate_async(prompt, stop=stop, **kwargs)
        yield LLMResponseChunk(
            delta_content=response.content,
            model=response.model,
            finish_reason="stop",
            usage=response.usage,
        )
