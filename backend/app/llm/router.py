"""Dynamic model router.

Selection order for a request:
1. explicit model id (validated against the catalogue and provider availability)
2. agent-configured preferred model
3. policy-based selection by required capability / tier / cost ceiling
4. fallback chain across configured providers

Every call is metered: tokens, cost, latency and errors are recorded to Prometheus
and to the cost ledger through the caller-supplied sink.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError, is_transient
from app.core.logging import get_logger
from app.core.metrics import llm_cost_usd_total, llm_errors_total, llm_latency, llm_tokens_total
from app.core.resilience import CircuitBreaker as Breaker
from app.core.resilience import RetryPolicy, get_breaker, with_retry
from app.core.runtime_config import runtime_config
from app.llm.base import LLMProvider
from app.llm.catalog import CATALOG, ModelSpec, compute_cost, resolve_model
from app.llm.jsonio import extract_json_object
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.bedrock import BedrockProvider
from app.llm.providers.google import GoogleProvider
from app.llm.providers.openai_compat import (
    build_azure_openai,
    build_deepseek,
    build_mistral,
    build_ollama,
    build_openai,
    build_together,
)
from app.llm.types import EmbeddingResult, LLMResponse, Message, StreamChunk, ToolSchema

log = get_logger("llm.router")

CostSink = Callable[[dict[str, Any]], Any]

#: Statuses that mean "not this model", as distinct from "not right now". OpenAI answers
#: 404 for a model the account cannot reach; 403 carries the same meaning when the id is
#: real but unentitled.
_MODEL_REJECTED_STATUSES = frozenset({403, 404})

#: The variable an operator has to set for each provider. "Unavailable" on its own leaves
#: them guessing which of nine credentials is the missing one.
PROVIDER_KEY_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY (and AZURE_OPENAI_ENDPOINT)",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}

#: How a provider says "that model is not yours to call". Distinct from a wrong base URL,
#: which also answers 404 -- hence matching the wording and not the status alone.
_UNAVAILABLE_PHRASES = (
    "does not exist",
    "do not have access",
    "does not have access",
    "model_not_found",
    "unknown model",
    "no such model",
    "not found for api version",
    "model is not supported",
    "invalid model",
)


#: Ceiling on models tried in one call. Without it an account entitled to nothing walks the
#: whole catalogue on every request instead of failing quickly and saying so.
MAX_CANDIDATES = 4


def _is_model_unavailable(exc: BaseException) -> bool:
    if not isinstance(exc, ProviderError):
        return False
    if exc.provider_status not in {400, 403, 404}:
        return False
    text = str(exc).lower()
    return any(phrase in text for phrase in _UNAVAILABLE_PHRASES)


class ModelRouter:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._cost_sinks: list[CostSink] = []
<<<<<<< HEAD
        #: Models the provider itself has said it does not serve to this account. The
        #: catalogue is a static price list, so it lists models an account may have no
        #: entitlement to; only the provider can settle that, and it settles it the same
        #: way on every call. Remembering the answer keeps selection away from a model
        #: that can never work, instead of re-deriving the same 404 on every request.
        self._unavailable_models: dict[str, str] = {}
=======
        #: Models the provider has rejected as nonexistent or not entitled for this account.
        self._unavailable: dict[str, str] = {}
>>>>>>> fe4ea56e593a7d3fc9794a1cba93f7a50c77a964
        self._build()

    def _build(self) -> None:
        self._providers = {
            "openai": build_openai(),
            "azure_openai": build_azure_openai(),
            "anthropic": AnthropicProvider(),
            "google": GoogleProvider(),
            "bedrock": BedrockProvider(),
            "mistral": build_mistral(),
            "deepseek": build_deepseek(),
            "together": build_together(),
            "ollama": build_ollama(),
        }

    def reload(self) -> None:
        # New credentials may carry different entitlements, so last run's answers expire.
        self._unavailable_models.clear()
        self._build()

    def register_cost_sink(self, sink: CostSink) -> None:
        self._cost_sinks.append(sink)

    # --- availability -------------------------------------------------------
    def provider(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

    def configured_providers(self) -> list[str]:
        """Providers whose settings are present. Says nothing about whether they work."""
        return [name for name, p in self._providers.items() if p.configured]

    def usable_providers(self, *, embeddings: bool | None = False) -> list[str]:
        """Providers that are configured *and* not currently circuit-broken.

        `configured` only means the settings are non-empty, so a rotated, mistyped or
        placeholder credential still counts as configured. Callers that need to know
        whether a model call has any chance of succeeding must ask this instead.
        """
        from app.core.resilience import get_breaker

        return [
            name
            for name in self.configured_providers()
            if get_breaker(f"llm:{name}").state != "open" and self._has_serviceable_model(name, embeddings)
        ]

    def _has_serviceable_model(self, provider: str, embeddings: bool | None = False) -> bool:
        """False once the provider has disowned every model of this kind that it lists.

        A key that authenticates but is entitled to nothing is, for the question "can this
        call succeed", the same as no key at all -- and callers that fail closed on an
        unusable provider need it reported unusable rather than merely broken. The kind
        matters: an account entitled to embeddings but no chat model is usable for one and
        not the other, and collapsing that distinction takes embeddings down with chat.
        """
        return any(
            m.provider == provider
            and m.id not in self._unavailable_models
            and (embeddings is None or m.is_embedding == embeddings)
            for m in CATALOG.values()
        )

    def mark_model_unavailable(self, model_id: str, reason: str) -> None:
        if model_id in self._unavailable_models:
            return
        self._unavailable_models[model_id] = reason
        log.error("model_unavailable", model=model_id, reason=reason[:200])

    def unavailable_models(self) -> dict[str, str]:
        return dict(self._unavailable_models)

    def readiness(self) -> dict[str, Any]:
        """Can a model be called right now, and if not, exactly what is missing.

        Checked before work is dispatched rather than discovered inside it: without this a
        missing credential fails every attempt of every call in the run first, which reads
        as a provider outage and sends the operator looking for one.
        """
        configured = self.configured_providers()
        usable = self.usable_providers()
        blocked = sorted(set(configured) - set(usable))
        return {
            "ready": bool(usable),
            "configured": configured,
            "usable": usable,
            "circuit_open": blocked,
            "set_one_of": sorted(set(PROVIDER_KEY_ENV_VARS.values())) if not configured else [],
            "reason": (
                None
                if usable
                else "every configured provider is circuit-open"
                if configured
                else "no provider credential is set"
            ),
        }

    def available_models(
        self, *, embeddings: bool | None = None, usable_only: bool = False
    ) -> list[ModelSpec]:
<<<<<<< HEAD
        configured = set(
            self.usable_providers(embeddings=embeddings) if usable_only else self.configured_providers()
        )
        models = [
            m for m in CATALOG.values() if m.provider in configured and m.id not in self._unavailable_models
        ]
=======
        configured = set(self.usable_providers() if usable_only else self.configured_providers())
        models = [m for m in CATALOG.values() if m.provider in configured and m.id not in self._unavailable]
>>>>>>> fe4ea56e593a7d3fc9794a1cba93f7a50c77a964
        if embeddings is True:
            return [m for m in models if m.is_embedding]
        if embeddings is False:
            return [m for m in models if not m.is_embedding]
        return models

    def is_available(self, model_id: str) -> bool:
        spec = resolve_model(model_id)
        return bool(
            spec
            and (self._providers.get(spec.provider) or None)
            and self._providers[spec.provider].configured
        )

    # --- selection ----------------------------------------------------------
    def select(
        self,
        *,
        model: str | None = None,
        tier: str | None = None,
        needs_tools: bool = False,
        needs_vision: bool = False,
        max_cost_per_mtok: float | None = None,
        embeddings: bool = False,
    ) -> ModelSpec:
        if model:
            spec = resolve_model(model)
            if spec is None:
                raise ProviderError(
                    f"Unknown model '{model}'", details={"known_models": sorted(CATALOG)[:40]}
                )
            provider = self._providers.get(spec.provider)
            if spec.id in self._unavailable_models:
                # Honouring the pin here would hand back the same 404 on every request.
                log.warning(
                    "model_unavailable_falling_through",
                    model=spec.id,
                    reason=self._unavailable_models[spec.id][:200],
                )
            elif provider and provider.configured:
                return spec
            else:
                log.warning("model_provider_unconfigured", model=model, provider=spec.provider)

        # Prefer providers that can actually be called. Falling back to the merely-configured
        # set matters: when every breaker is open the caller should reach the provider and get
        # its real error back, rather than a "nothing is configured" message naming credentials
        # that are in fact already set.
        candidates = self.available_models(embeddings=embeddings, usable_only=True)
        if not candidates:
            candidates = self.available_models(embeddings=embeddings)
        if needs_tools:
            candidates = [m for m in candidates if m.supports_tools]
        if needs_vision:
            candidates = [m for m in candidates if m.supports_vision]
        if tier:
            tiered = [m for m in candidates if m.tier == tier]
            candidates = tiered or candidates
        if max_cost_per_mtok is not None:
            affordable = [m for m in candidates if m.input_price_per_mtok <= max_cost_per_mtok]
            candidates = affordable or candidates
        if not candidates:
            wanted = resolve_model(model) if model else None
<<<<<<< HEAD
            retired = self._unavailable_models
            # Telling an operator to set a key they have already set sends them to fix the
            # wrong thing. If the catalogue emptied because the provider disowned the
            # models, say that instead -- the fix is an entitlement, not a credential.
            if retired and self.configured_providers():
                raise ProviderNotConfiguredError(
                    "Every model this account can be offered was refused by its provider",
                    details={
                        "refused": {k: v[:180] for k, v in list(retired.items())[:8]},
                        "configured_providers": self.configured_providers(),
                        "hint": "Check the model entitlements on the key, or set DEFAULT_MODEL "
                        "and GUARDRAILS_MODEL to a model the account can actually call.",
                        "requested_model": model,
=======
            if self._unavailable and self.configured_providers():
                # The credentials are fine; the account cannot call any catalogued model.
                # Saying "no provider is configured" here would send the operator to add a
                # key they already have.
                raise ProviderNotConfiguredError(
                    "Every model this deployment can select has been rejected by its provider",
                    details={
                        "rejected": self.unavailable_models,
                        "configured": self.configured_providers(),
                        "hint": "Set DEFAULT_MODEL (and GUARDRAILS_MODEL) to a model the "
                        "account may call, or grant access to one of the rejected models",
>>>>>>> fe4ea56e593a7d3fc9794a1cba93f7a50c77a964
                    },
                )
            raise ProviderNotConfiguredError(
                f"No provider is configured for model '{model}'"
                if wanted
                else "No LLM provider is configured",
                details={
                    "set_one_of": sorted(set(PROVIDER_KEY_ENV_VARS.values())),
                    "required_for_requested_model": (
                        PROVIDER_KEY_ENV_VARS.get(wanted.provider) if wanted else None
                    ),
                    "requested_model": model,
                },
            )
        candidates.sort(key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        preferred = resolve_model(runtime_config.default_model)
        if preferred and preferred in candidates and not (tier or max_cost_per_mtok):
            return preferred
        if tier == "frontier":
            return max(candidates, key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        return candidates[0]

    def fallback_chain(self, spec: ModelSpec, *, needs_tools: bool) -> list[ModelSpec]:
        # A fallback exists to route around a failing provider, so one that is already
        # circuit-open is not a fallback -- dispatching to it only adds a rejection.
        others = [
            m
            for m in self.available_models(embeddings=spec.is_embedding, usable_only=True)
            if m.id != spec.id and m.tier == spec.tier and (m.supports_tools or not needs_tools)
        ]
        others.sort(key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        return others[:2]

    # --- metering -----------------------------------------------------------
    async def _meter(self, response: LLMResponse, spec: ModelSpec, context: dict[str, Any]) -> None:
        response.cost_usd = compute_cost(
            spec,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.usage.cached_input_tokens,
        )
        llm_tokens_total.labels(spec.provider, spec.id, "input").inc(response.usage.input_tokens)
        llm_tokens_total.labels(spec.provider, spec.id, "output").inc(response.usage.output_tokens)
        if response.usage.cached_input_tokens:
            llm_tokens_total.labels(spec.provider, spec.id, "cached").inc(response.usage.cached_input_tokens)
        llm_cost_usd_total.labels(spec.provider, spec.id).inc(response.cost_usd)
        llm_latency.labels(spec.provider, spec.id).observe(response.latency_ms / 1000)
        record = {
            "category": "embedding" if spec.is_embedding else "llm",
            "provider": spec.provider,
            "model": spec.id,
            "tokens_input": response.usage.input_tokens,
            "tokens_output": response.usage.output_tokens,
            "tokens_cached": response.usage.cached_input_tokens,
            "cost_usd": response.cost_usd,
            "latency_ms": response.latency_ms,
            **context,
        }
        for sink in self._cost_sinks:
            try:
                result = sink(record)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # pragma: no cover - metering must never break a call
                log.warning("cost_sink_failed", error=str(exc))

    # --- calls --------------------------------------------------------------
    async def chat(
        self,
        *,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        top_p: float | None = None,
        stop: list[str] | None = None,
        json_mode: bool = False,
        tier: str | None = None,
        allow_fallback: bool = True,
        context: dict[str, Any] | None = None,
    ) -> LLMResponse:
        spec = self.select(model=model, tier=tier, needs_tools=bool(tools))
        queue = [spec] + (self.fallback_chain(spec, needs_tools=bool(tools)) if allow_fallback else [])
        last_error: Exception | None = None
        attempted: list[tuple[str, BaseException]] = []
        tried: set[str] = set()

        while queue and len(attempted) < MAX_CANDIDATES:
            candidate = queue.pop(0)
            if candidate.id in tried:
                continue
            tried.add(candidate.id)
            provider = self._providers[candidate.provider]
            breaker = get_breaker(f"llm:{candidate.provider}", failure_threshold=5, recovery_seconds=30)

            # Bound through a factory rather than captured: the retry closure must see this
            # iteration's provider, not whichever one the loop has moved on to.
            def _attempt(
                p: LLMProvider = provider,
                c: ModelSpec = candidate,
                b: Breaker = breaker,
            ) -> Awaitable[LLMResponse]:
                async def _call() -> LLMResponse:
                    return await p.chat(
                        model=c.id,
                        messages=messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=min(max_tokens, c.max_output_tokens or max_tokens),
                        top_p=top_p,
                        stop=stop,
                        json_mode=json_mode and c.supports_json_mode,
                    )

                return b.call(_call)

            try:
                response = await with_retry(
                    _attempt,
                    RetryPolicy(
                        max_attempts=settings.llm_max_retries,
                        base_delay=0.6,
                        give_up_on=(ProviderNotConfiguredError,),
                    ),
                    name=f"llm:{candidate.id}",
                )
                await self._meter(response, candidate, context or {})
                if candidate.id != spec.id:
                    log.warning("llm_fallback_used", requested=spec.id, used=candidate.id)
                return response
            except Exception as exc:
                last_error = exc
                attempted.append((candidate.id, exc))
                llm_errors_total.labels(candidate.provider, candidate.id, type(exc).__name__).inc()
                log.error("llm_call_failed", model=candidate.id, provider=candidate.provider, error=str(exc))
<<<<<<< HEAD
                # A 404 on a completion is the provider disowning the model id, not a fault
                # in the request or a passing outage. Retrying it or trying it again next
                # request only reproduces it, and while the model stays selectable every
                # caller that fails closed on a model error stays broken.
                if isinstance(exc, ProviderError) and exc.provider_status in _MODEL_REJECTED_STATUSES:
                    self.mark_model_unavailable(candidate.id, str(exc))
=======
                if _is_model_unavailable(exc):
                    self.demote(candidate.id, reason=str(exc))
                    if allow_fallback:
                        # The fallback chain is tier-locked, which is right for an outage --
                        # you want comparable capability. It is wrong here: a model the
                        # account may not call says nothing about capability, and staying in
                        # its tier means never reaching the tier that does work. Any usable
                        # model beats no answer.
                        queue.extend(
                            m
                            for m in self._by_price(embeddings=False, needs_tools=bool(tools))
                            if m.id not in tried
                        )
>>>>>>> fe4ea56e593a7d3fc9794a1cba93f7a50c77a964
        assert last_error is not None
        raise self._chain_error(attempted, last_error)

    def _by_price(self, *, embeddings: bool, needs_tools: bool) -> list[ModelSpec]:
        models = [
            m
            for m in self.available_models(embeddings=embeddings, usable_only=True)
            if m.supports_tools or not needs_tools
        ]
        models.sort(key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        return models

    @staticmethod
    def _chain_error(attempted: list[tuple[str, BaseException]], last: BaseException) -> BaseException:
        """One error naming every model that was tried, not just whichever failed last.

        Raising only the last one describes a model the caller never asked for -- the fallback
        -- and silently discards why the model it *did* ask for failed. Someone reading
        "gpt-5.5-mini does not exist" has no way to know a different model was tried first, so
        they go looking for the wrong problem.
        """
        if len(attempted) < 2:
            return last
        detail = "; ".join(f"{model}: {exc}" for model, exc in attempted)
        return ProviderError(
            f"All {len(attempted)} candidate models failed - {detail}",
            details={"attempted": [model for model, _ in attempted]},
            retryable=is_transient(last),
        )

    # --- model availability -------------------------------------------------
    def demote(self, model_id: str, *, reason: str) -> None:
        """Stop selecting a model the provider says this account cannot call.

        The catalogue is a price list, not an entitlement list: which of its models a given
        key may use is between the account and the provider, and only the provider can say.
        Without this the router re-picks the same rejected model on every call, so one
        inaccessible entry costs a wasted round trip forever rather than once.
        """
        if model_id in self._unavailable:
            return
        self._unavailable[model_id] = reason
        spec = resolve_model(model_id)
        log.error(
            "model_unavailable_for_account",
            model=model_id,
            provider=spec.provider if spec else "unknown",
            reason=reason[:200],
            hint="pin an accessible model with DEFAULT_MODEL, or GUARDRAILS_MODEL for the rails",
        )

    @property
    def unavailable_models(self) -> dict[str, str]:
        return dict(self._unavailable)

    def restore(self, model_id: str | None = None) -> None:
        """Clear a demotion, for when access is granted without restarting the process."""
        if model_id is None:
            self._unavailable.clear()
        else:
            self._unavailable.pop(model_id, None)

    async def chat_json(
        self, *, messages: list[Message], **kwargs: Any
    ) -> tuple[dict[str, Any] | None, LLMResponse]:
        """Chat and parse an object out of the reply, with one corrective call if needed.

        The repair round-trip is worth its cost: the alternative is a caller that silently
        falls back to an empty structure, which produces a plausible-looking run with none of
        the requested reasoning in it and no error to explain why. The returned response
        carries the summed tokens and cost of both calls -- charging only the second would
        under-report the run.
        """
        kwargs.setdefault("json_mode", True)
        response = await self.chat(messages=messages, **kwargs)
        parsed = extract_json_object(response.content)
        if parsed is not None:
            return parsed, response

        log.warning(
            "json_reply_unparseable",
            model=response.model,
            chars=len(response.content or ""),
            # The usual cause of an unparseable object is an object cut in half. Repairing
            # under the same ceiling truncates again, so the ceiling is the thing to raise.
            truncated=response.truncated,
        )
        repair = await self.chat(
            messages=[
                *messages,
                Message(role="assistant", content=response.content or ""),
                Message(
                    role="user",
                    content=(
                        "That reply was not valid JSON. Reply again with the same content as a "
                        "single JSON object and nothing else -- no prose, no code fence."
                    ),
                ),
            ],
            **kwargs,
        )
        repair.usage.input_tokens += response.usage.input_tokens
        repair.usage.output_tokens += response.usage.output_tokens
        repair.usage.cached_input_tokens += response.usage.cached_input_tokens
        repair.cost_usd += response.cost_usd
        repair.latency_ms += response.latency_ms
        return extract_json_object(repair.content), repair

    async def stream(
        self,
        *,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tier: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        spec = self.select(model=model, tier=tier, needs_tools=bool(tools))
        provider = self._providers[spec.provider]
        async for chunk in provider.stream(
            model=spec.id,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=min(max_tokens, spec.max_output_tokens or max_tokens),
        ):
            if chunk.done and chunk.response is not None:
                await self._meter(chunk.response, spec, context or {})
            yield chunk

    async def embed(
        self, *, texts: list[str], model: str | None = None, context: dict[str, Any] | None = None
    ) -> EmbeddingResult:
        spec = self.select(model=model or settings.default_embedding_model, embeddings=True)
        provider = self._providers[spec.provider]
        breaker = get_breaker(f"embed:{spec.provider}")
        result = await with_retry(
            lambda: breaker.call(lambda: provider.embed(model=spec.id, texts=texts)),
            RetryPolicy(max_attempts=2, give_up_on=(ProviderNotConfiguredError,)),
            name=f"embed:{spec.id}",
        )
        result.cost_usd = compute_cost(spec, result.usage.input_tokens, 0)
        llm_tokens_total.labels(spec.provider, spec.id, "embedding").inc(result.usage.input_tokens)
        llm_cost_usd_total.labels(spec.provider, spec.id).inc(result.cost_usd)
        for sink in self._cost_sinks:
            try:
                out = sink(
                    {
                        "category": "embedding",
                        "provider": spec.provider,
                        "model": spec.id,
                        "tokens_input": result.usage.input_tokens,
                        "tokens_output": 0,
                        "cost_usd": result.cost_usd,
                        "latency_ms": result.latency_ms,
                        **(context or {}),
                    }
                )
                if asyncio.iscoroutine(out):
                    await out
            except Exception:
                pass
        return result

    async def health(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(p.health() for p in self._providers.values())))


router = ModelRouter()
