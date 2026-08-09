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
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.core.logging import get_logger
from app.core.metrics import llm_cost_usd_total, llm_errors_total, llm_latency, llm_tokens_total
from app.core.resilience import RetryPolicy, get_breaker, with_retry
from app.llm.base import LLMProvider
from app.llm.catalog import CATALOG, ModelSpec, compute_cost, resolve_model
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


class ModelRouter:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._cost_sinks: list[CostSink] = []
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
        self._build()

    def register_cost_sink(self, sink: CostSink) -> None:
        self._cost_sinks.append(sink)

    # --- availability -------------------------------------------------------
    def provider(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

    def configured_providers(self) -> list[str]:
        """Providers whose settings are present. Says nothing about whether they work."""
        return [name for name, p in self._providers.items() if p.configured]

    def usable_providers(self) -> list[str]:
        """Providers that are configured *and* not currently circuit-broken.

        `configured` only means the settings are non-empty, so a rotated, mistyped or
        placeholder credential still counts as configured. Callers that need to know
        whether a model call has any chance of succeeding must ask this instead.
        """
        from app.core.resilience import get_breaker

        return [
            name for name in self.configured_providers()
            if get_breaker(f"llm:{name}").state != "open"
        ]

    def available_models(self, *, embeddings: bool | None = None) -> list[ModelSpec]:
        configured = set(self.configured_providers())
        models = [m for m in CATALOG.values() if m.provider in configured]
        if embeddings is True:
            return [m for m in models if m.is_embedding]
        if embeddings is False:
            return [m for m in models if not m.is_embedding]
        return models

    def is_available(self, model_id: str) -> bool:
        spec = resolve_model(model_id)
        return bool(spec and (self._providers.get(spec.provider) or None) and
                    self._providers[spec.provider].configured)

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
                raise ProviderError(f"Unknown model '{model}'",
                                    details={"known_models": sorted(CATALOG)[:40]})
            provider = self._providers.get(spec.provider)
            if provider and provider.configured:
                return spec
            log.warning("model_provider_unconfigured", model=model, provider=spec.provider)

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
            raise ProviderNotConfiguredError(
                "No LLM provider is configured",
                details={
                    "hint": "Set at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, "
                            "AZURE_OPENAI_API_KEY, AWS_ACCESS_KEY_ID, MISTRAL_API_KEY, "
                            "DEEPSEEK_API_KEY, TOGETHER_API_KEY or OLLAMA_BASE_URL",
                    "requested_model": model,
                },
            )
        candidates.sort(key=lambda m: (m.input_price_per_mtok + m.output_price_per_mtok))
        preferred = resolve_model(settings.default_model)
        if preferred and preferred in candidates and not (tier or max_cost_per_mtok):
            return preferred
        if tier == "frontier":
            return max(candidates, key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        return candidates[0]

    def fallback_chain(self, spec: ModelSpec, *, needs_tools: bool) -> list[ModelSpec]:
        others = [
            m
            for m in self.available_models(embeddings=spec.is_embedding)
            if m.id != spec.id and m.tier == spec.tier and (m.supports_tools or not needs_tools)
        ]
        others.sort(key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)
        return others[:2]

    # --- metering -----------------------------------------------------------
    async def _meter(self, response: LLMResponse, spec: ModelSpec, context: dict[str, Any]) -> None:
        response.cost_usd = compute_cost(
            spec, response.usage.input_tokens, response.usage.output_tokens,
            response.usage.cached_input_tokens,
        )
        llm_tokens_total.labels(spec.provider, spec.id, "input").inc(response.usage.input_tokens)
        llm_tokens_total.labels(spec.provider, spec.id, "output").inc(response.usage.output_tokens)
        if response.usage.cached_input_tokens:
            llm_tokens_total.labels(spec.provider, spec.id, "cached").inc(
                response.usage.cached_input_tokens
            )
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
        chain = [spec] + (self.fallback_chain(spec, needs_tools=bool(tools)) if allow_fallback else [])
        last_error: Exception | None = None

        for candidate in chain:
            provider = self._providers[candidate.provider]
            breaker = get_breaker(f"llm:{candidate.provider}", failure_threshold=5,
                                  recovery_seconds=30)

            async def _call(p: LLMProvider = provider, c: ModelSpec = candidate) -> LLMResponse:
                return await p.chat(
                    model=c.id, messages=messages, tools=tools, temperature=temperature,
                    max_tokens=min(max_tokens, c.max_output_tokens or max_tokens),
                    top_p=top_p, stop=stop, json_mode=json_mode and c.supports_json_mode,
                )

            try:
                response = await with_retry(
                    lambda p=provider, c=candidate, b=breaker: b.call(lambda: _call(p, c)),
                    RetryPolicy(max_attempts=settings.llm_max_retries, base_delay=0.6,
                                give_up_on=(ProviderNotConfiguredError,)),
                    name=f"llm:{candidate.id}",
                )
                await self._meter(response, candidate, context or {})
                if candidate.id != spec.id:
                    log.warning("llm_fallback_used", requested=spec.id, used=candidate.id)
                return response
            except Exception as exc:
                last_error = exc
                llm_errors_total.labels(candidate.provider, candidate.id, type(exc).__name__).inc()
                log.error("llm_call_failed", model=candidate.id, provider=candidate.provider,
                          error=str(exc))
        assert last_error is not None
        raise last_error

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
            model=spec.id, messages=messages, tools=tools, temperature=temperature,
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
                out = sink({
                    "category": "embedding", "provider": spec.provider, "model": spec.id,
                    "tokens_input": result.usage.input_tokens, "tokens_output": 0,
                    "cost_usd": result.cost_usd, "latency_ms": result.latency_ms,
                    **(context or {}),
                })
                if asyncio.iscoroutine(out):
                    await out
            except Exception:
                pass
        return result

    async def health(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(p.health() for p in self._providers.values())))


router = ModelRouter()
