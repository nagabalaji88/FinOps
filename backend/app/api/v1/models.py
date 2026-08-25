"""Model administration: what can be called, what is selected, and which keys are set.

Three questions an operator has to be able to answer without a deploy:

* which models can this deployment actually call, according to the providers themselves
* which one do the agents and the guardrails use
* which credentials are set, and do they work

Everything here writes through ``runtime_config``, so a change applies to the next model call
rather than the next restart. Key *values* are never returned -- only whether one is present,
where it came from, and its last four characters.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.rbac import Permission
from app.core.redaction import client_safe_error
from app.core.runtime_config import PROVIDER_ENV, runtime_config
from app.llm.catalog import CATALOG, compute_cost, resolve_model
from app.llm.router import PROVIDER_KEY_ENV_VARS
from app.llm.router import router as model_router
from app.llm.types import Message

log = get_logger("api.models")

router = APIRouter(prefix="/models", tags=["models"])

#: A prompt short enough to cost nothing and specific enough that a wrong answer is obvious.
PROBE = "Reply with the single word: ready"


def _spec_payload(spec: Any, configured: set[str], unavailable: dict[str, str]) -> dict[str, Any]:
    return {
        "id": spec.id,
        "display_name": spec.display_name,
        "provider": spec.provider,
        "family": spec.family,
        "tier": spec.tier,
        "context_window": spec.context_window,
        "max_output_tokens": spec.max_output_tokens,
        "input_price_per_mtok": spec.input_price_per_mtok,
        "output_price_per_mtok": spec.output_price_per_mtok,
        "supports_tools": spec.supports_tools,
        "supports_vision": spec.supports_vision,
        "supports_json_mode": spec.supports_json_mode,
        "is_embedding": spec.is_embedding,
        "tags": list(spec.tags),
        "credential_env_var": PROVIDER_KEY_ENV_VARS.get(spec.provider),
        # Three separate answers, deliberately. A model can be catalogued but have no
        # credential; have one and still be refused by the account; or be local and need
        # none at all. Collapsing them into "unavailable" leaves the operator guessing.
        "credential_available": spec.provider in configured,
        "rejected_by_provider": unavailable.get(spec.id),
        "is_local_runtime": spec.provider == "ollama",
    }


@router.get("")
async def list_catalogue(principal: PrincipalDep) -> dict[str, Any]:
    """The priced catalogue, with what is selected and what each model would need."""
    principal.require(Permission.SERVICE_READ)
    configured = set(model_router.configured_providers())
    unavailable = model_router.unavailable_models()
    return {
        "selection": {
            "default_model": runtime_config.default_model or None,
            "guardrails_model": runtime_config.guardrails_model or None,
            "effective_guardrails_model": (
                runtime_config.guardrails_model or runtime_config.default_model or None
            ),
            "aligned": not runtime_config.guardrails_model
            or runtime_config.guardrails_model == runtime_config.default_model,
        },
        "readiness": model_router.readiness(),
        "models": [_spec_payload(s, configured, unavailable) for s in CATALOG.values()],
    }


@router.get("/available")
async def live_models(principal: PrincipalDep) -> dict[str, Any]:
    """What each credential can actually call, asked of the providers right now.

    Slower than the catalogue because it is a network round trip per provider, and worth it:
    this is the list that does not go stale when a provider ships a model or an account's
    entitlements change.
    """
    principal.require(Permission.SERVICE_READ)
    names = model_router.configured_providers()

    async def probe(name: str) -> dict[str, Any]:
        provider = model_router.provider(name)
        if provider is None:
            return {"provider": name, "status": "unknown", "models": []}
        started = time.perf_counter()
        try:
            ids = await provider.list_models()
        except Exception as exc:
            return {
                "provider": name,
                "status": "error",
                "error": client_safe_error(str(exc)),
                "models": [],
            }
        return {
            "provider": name,
            "status": "ok" if ids else "no_listing_endpoint",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "models": [
                {"id": model_id, "in_catalogue": resolve_model(model_id) is not None} for model_id in ids
            ],
        }

    results = list(await asyncio.gather(*(probe(name) for name in names)))
    return {
        "providers": results,
        "total": sum(len(r["models"]) for r in results),
        "note": "Fetched from each provider, so this is what your keys can call - not a static list",
    }


async def _provider_lists(model_id: str) -> bool:
    """True when some configured provider names this model in its own listing."""
    for name in model_router.configured_providers():
        provider = model_router.provider(name)
        if provider is None:
            continue
        try:
            if model_id in await provider.list_models():
                return True
        except Exception as exc:  # one unreachable provider must not veto the others
            log.debug("listing_failed_during_validation", provider=name, error=str(exc))
    return False


class ModelSelection(BaseModel):
    default_model: str | None = Field(default=None, description="Model the agents use")
    guardrails_model: str | None = Field(
        default=None, description="Model the rails judge with; empty follows default_model"
    )


@router.put("/selection")
async def set_selection(
    payload: ModelSelection, request: Request, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    """Choose the model the agents run on. Applies to the next call, not the next restart."""
    principal.require(Permission.SECURITY_ADMIN)

    for field, value in (
        ("default_model", payload.default_model),
        ("guardrails_model", payload.guardrails_model),
    ):
        if value is None:
            continue
        # An empty string is a deliberate clear -- it hands the choice back to the router.
        if value and resolve_model(value) is None:
            # The catalogue is a price list maintained by hand, so it goes stale: a provider
            # retires a model, ships a replacement, and an operator whose key can call the
            # replacement cannot select it. Ask the provider instead. Accepting a model it
            # actually lists is a real check, not a bypass -- and it is the difference
            # between the platform being usable this week and after the next catalogue edit.
            if not await _provider_lists(value):
                raise ValidationError(
                    f"'{value}' is neither catalogued nor offered by any configured provider",
                    details={
                        "field": field,
                        "hint": "Check the Available models tab for what these keys can call",
                        "known_models": sorted(CATALOG)[:40],
                    },
                )
            log.warning(
                "uncatalogued_model_selected",
                model=value,
                note="reachable but unpriced; cost for this model will report as zero",
            )
        await runtime_config.set_model(session, field, value, actor=principal.email)

    await write_audit(
        session,
        principal=principal,
        action="model.selection.update",
        resource_type="platform",
        resource_id="model_selection",
        details={"default_model": payload.default_model, "guardrails_model": payload.guardrails_model},
        request=request,
    )
    await session.commit()
    return {
        "default_model": runtime_config.default_model or None,
        "guardrails_model": runtime_config.guardrails_model or None,
        "effective_guardrails_model": (
            runtime_config.guardrails_model or runtime_config.default_model or None
        ),
    }


class ModelTest(BaseModel):
    model_id: str = Field(min_length=1)
    provider: str | None = Field(
        default=None, description="Required only for a model the catalogue does not price"
    )


@router.post("/test")
async def test_model(payload: ModelTest, principal: PrincipalDep) -> dict[str, Any]:
    """Call this exact model once, with nothing between the request and the provider.

    Deliberately not routed through ``chat()``. Routing exists to get *an* answer: it
    substitutes when the requested model has no credential or has been retired, and refuses
    outright while the provider's breaker is open. Both are right for serving traffic and
    wrong here. The operator is asking whether this model works *right now*, and the only
    honest answers are its own reply or its own error -- a different model's success, or a
    breaker state left over from an earlier failure, answers a question nobody asked.

    The model travels in the body rather than the path. Ids legitimately contain slashes --
    ``meta-llama/Llama-3.3-70B-Instruct-Turbo`` -- so a path parameter needs the ``:path``
    converter, and that converter is greedy enough to swallow ``/keys/openai/test`` on its
    way past.
    """
    principal.require(Permission.PLAYGROUND_USE)
    model_id = payload.model_id
    spec = resolve_model(model_id)
    provider_name = spec.provider if spec else payload.provider
    if provider_name is None:
        raise NotFoundError(f"'{model_id}' is not in the catalogue. Pass its provider to test it anyway.")

    provider = model_router.provider(provider_name)
    if provider is None:
        raise NotFoundError(f"'{provider_name}' is not a provider this platform calls")
    if not provider.configured:
        return {
            "model": model_id,
            "provider": provider_name,
            "ok": False,
            "error": f"no credential for {provider_name} - set {PROVIDER_KEY_ENV_VARS.get(provider_name)}",
        }

    started = time.perf_counter()
    try:
        response = await provider.chat(
            model=model_id,
            messages=[Message(role="user", content=PROBE)],
            max_tokens=16,
            temperature=0.0,
        )
    except Exception as exc:
        log.warning("model_test_failed", model=model_id, provider=provider_name, error=str(exc))
        return {
            "model": model_id,
            "provider": provider_name,
            "ok": False,
            "error": client_safe_error(str(exc)),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # It answered, so whatever retired it earlier no longer holds -- an entitlement can be
    # granted between two calls, and nothing else would ever clear the record.
    model_router.restore(model_id)
    cost = compute_cost(spec, response.usage.input_tokens, response.usage.output_tokens) if spec else 0.0
    return {
        "model": model_id,
        "served_by": response.model,
        "provider": response.provider,
        "ok": True,
        "reply": response.content[:200],
        "latency_ms": round(response.latency_ms, 1),
        "tokens": {"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        "cost_usd": round(cost, 6),
        "priced": spec is not None,
        "truncated": response.truncated,
    }


@router.get("/keys")
async def list_keys(principal: PrincipalDep) -> list[dict[str, Any]]:
    """Which provider credentials are set and where each came from. Never the value."""
    principal.require(Permission.SECURITY_READ)
    configured = set(model_router.configured_providers())
    enabled_by: dict[str, int] = {}
    for spec in CATALOG.values():
        enabled_by[spec.provider] = enabled_by.get(spec.provider, 0) + 1
    return [
        {
            "provider": provider,
            "env_var": PROVIDER_KEY_ENV_VARS.get(provider),
            "source": runtime_config.key_source(provider),
            "set": runtime_config.api_key(provider) is not None,
            "usable": provider in configured,
            "catalogued_models": enabled_by.get(provider, 0),
        }
        for provider in PROVIDER_ENV
    ]


class ApiKeyUpdate(BaseModel):
    value: str = Field(min_length=1, description="The key. Stored encrypted; never returned.")


@router.put("/keys/{provider}")
async def set_key(
    provider: str,
    payload: ApiKeyUpdate,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    principal.require(Permission.SECURITY_ADMIN)
    if provider not in PROVIDER_ENV:
        raise NotFoundError(f"'{provider}' is not a provider this platform calls")
    await runtime_config.set_api_key(session, provider, payload.value.strip(), actor=principal.email)
    await write_audit(
        session,
        principal=principal,
        action="model.key.update",
        resource_type="provider_key",
        resource_id=provider,
        severity="warning",
        details={"provider": provider},  # deliberately no value, not even a hint
        request=request,
    )
    await session.commit()
    return {"provider": provider, "source": runtime_config.key_source(provider), "set": True}


@router.delete("/keys/{provider}")
async def clear_key(
    provider: str, request: Request, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    """Drop the stored key so the environment variable applies again."""
    principal.require(Permission.SECURITY_ADMIN)
    if provider not in PROVIDER_ENV:
        raise NotFoundError(f"'{provider}' is not a provider this platform calls")
    await runtime_config.clear_api_key(session, provider, actor=principal.email)
    await write_audit(
        session,
        principal=principal,
        action="model.key.clear",
        resource_type="provider_key",
        resource_id=provider,
        severity="warning",
        details={"provider": provider},
        request=request,
    )
    await session.commit()
    return {"provider": provider, "source": runtime_config.key_source(provider)}


@router.post("/keys/{provider}/test")
async def test_key(provider: str, principal: PrincipalDep) -> dict[str, Any]:
    """Ask the provider to list its models. Proves the key authenticates; says nothing about quota."""
    principal.require(Permission.SECURITY_READ)
    instance = model_router.provider(provider)
    if instance is None:
        raise NotFoundError(f"'{provider}' is not a provider this platform calls")
    if not instance.configured:
        return {
            "provider": provider,
            "ok": False,
            "error": f"no credential set - {PROVIDER_KEY_ENV_VARS.get(provider)}",
        }
    started = time.perf_counter()
    try:
        ids = await instance.list_models()
    except Exception as exc:
        return {"provider": provider, "ok": False, "error": client_safe_error(str(exc))}
    return {
        "provider": provider,
        "ok": True,
        "reachable_models": len(ids),
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "note": "key authenticates; this does not check remaining quota",
    }
