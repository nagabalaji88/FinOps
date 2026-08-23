"""Bridges NeMo Guardrails onto the platform's model router.

NeMo's LLM-backed rails (self-check input/output, fact checking) need a model. Rather
than give NeMo its own provider credentials and its own HTTP client, this adapter
implements NeMo's framework-agnostic ``LLMModel`` protocol on top of `model_router`, so
rail calls go through the same routing, circuit breakers, retries, fallback chain and cost
metering as every other model call the platform makes — and land in the same cost ledger.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any, cast

from app.core.logging import get_logger
from app.core.runtime_config import runtime_config
from app.llm.router import router as model_router
from app.llm.types import Message, Role

#: The four roles `Message` accepts; anything else from a rail is treated as a user turn.
ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})

log = get_logger("guardrails.llm")

# Rails are classifiers, not writers: deterministic and short.
RAIL_TEMPERATURE = 0.0
RAIL_MAX_TOKENS = 256

#: A rail prompt that ends by demanding a bare verdict. Only those replies are normalised --
#: fact-checking and dialogue tasks go through untouched.
_ASKS_FOR_VERDICT = re.compile(r"answer\s+with\s+only\s*[\"']?\s*yes[\"']?\s*or\s*[\"']?\s*no", re.I)

#: NeMo's `is_content_safe` reads only the FIRST TWO WORDS of the reply and blocks anything it
#: does not recognise there. So "no" is allowed, and "Based on the policy, no." is refused --
#: the model classified correctly and the request is blocked anyway, which reads to the
#: operator as an unsafe request rather than as a parsing artifact. The rail prompt asks for a
#: bare verdict, so extracting one is faithful to its intent; this hands NeMo the answer the
#: prompt asked for.
_BLOCK_TOKENS = frozenset({"yes", "unsafe", "block", "blocked", "violation", "violates"})
_ALLOW_TOKENS = frozenset({"no", "safe", "allow", "allowed", "compliant", "permitted"})
_NEGATORS = frozenset({"not", "isnt", "arent", "no", "never", "dont", "doesnt", "cannot", "cant"})


def normalise_verdict(reply: str) -> str | None:
    """Reduce a yes/no rail reply to "yes" (block) or "no" (allow).

    Returns None when the reply carries no verdict at all, which is a broken classifier
    rather than a decision about the content -- the caller decides what to do with that.
    """
    words = re.sub(r"[^\w\s]+", " ", (reply or "").lower()).split()
    for index, word in enumerate(words):
        if word in _BLOCK_TOKENS:
            verdict = "yes"
        elif word in _ALLOW_TOKENS:
            verdict = "no"
        else:
            continue
        # "not safe" and "no violation" invert; taking the bare token would read each of
        # them backwards, which is the one error a rail must never make.
        previous = words[index - 1] if index else ""
        if previous in _NEGATORS and previous != word:
            verdict = "no" if verdict == "yes" else "yes"
        return verdict
    return None


def _to_messages(prompt: Any) -> list[Message]:
    """NeMo passes either a plain string or its own ChatMessage list."""
    if isinstance(prompt, str):
        return [Message(role="user", content=prompt)]

    messages: list[Message] = []
    for item in prompt:
        role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else None)
        content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None)
        raw_role = str(getattr(role, "value", role) or "user")
        role_value: Role = cast(Role, raw_role) if raw_role in ROLES else "user"
        if isinstance(content, list):  # multi-part content: keep the text parts
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        messages.append(Message(role=role_value, content=str(content or "")))
    return messages or [Message(role="user", content="")]


class RouterLLM:
    """NeMo ``LLMModel`` backed by the platform's model router."""

    def __init__(self, *, model: str | None = None, context: dict[str, Any] | None = None):
        # GUARDRAILS_MODEL, then DEFAULT_MODEL, then let the router choose.
        #
        # Falling straight through to the router when GUARDRAILS_MODEL is unset -- which is
        # what this did, despite the setting documenting otherwise -- makes the rails pick the
        # cheapest catalogued model independently of the agents. An operator who sets
        # DEFAULT_MODEL to a model their key can call still gets rails on a different one,
        # and the run fails at the rail with a model nobody chose. Alignment is the default;
        # GUARDRAILS_MODEL stays available to put rails on something smaller on purpose.
        self._model = model or runtime_config.guardrails_model or runtime_config.default_model or ""
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
        content = self._verdict_or_text(prompt, response.content)
        return LLMResponse(
            content=content,
            model=response.model,
            finish_reason="stop",
            request_id=response.request_id,
            usage=UsageInfo(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total,
            ),
        )

    def _verdict_or_text(self, prompt: Any, content: str) -> str:
        """Canonicalise a yes/no rail answer; pass anything else through unchanged."""
        text = "\n".join(m.content for m in _to_messages(prompt))
        if not _ASKS_FOR_VERDICT.search(text):
            return content

        verdict = normalise_verdict(content)
        if verdict is None:
            # Fail closed, but say why. A classifier that answers neither yes nor no is
            # malfunctioning, and logging that is the difference between an operator fixing
            # the model and an operator hunting for the policy their request supposedly hit.
            log.warning(
                "rail_verdict_unparseable",
                model=self._model or "router-selected",
                reply=(content or "")[:200],
            )
            return "yes"
        if verdict != (content or "").strip().lower():
            log.debug("rail_verdict_normalised", verdict=verdict, reply=(content or "")[:120])
        return verdict

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
