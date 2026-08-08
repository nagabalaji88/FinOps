"""Deterministic rail actions invoked from Colang.

NeMo's own rails (self check input/output, fact checking) need a model. These do not:
they are pattern checks over the request and the response, so the rails still hold when no
provider is configured.

Each check returns ``None`` when the text is acceptable, or a ``"rule|detail"`` string when
it is not — Colang branches on truthiness, and the rule name travels into the rail
exception so a blocked run says exactly which control fired.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from app.guardrails.patterns import (
    COLLECTIONS_BYPASS_PATTERNS,
    COLLECTIONS_THREAT_PATTERNS,
    CONTROL_BYPASS_PATTERNS,
    EVASION_PATTERNS,
    PII_PATTERNS,
    PROHIBITED_CREDIT_FACTORS,
    PROMPT_INJECTION_PATTERNS,
    TIPPING_OFF_OUTPUT_PATTERNS,
    TIPPING_OFF_PATTERNS,
    UNLICENSED_ADVICE_PATTERNS,
    first_match,
    mask,
)

log = get_logger("guardrails.actions")


def fail_closed(fn: Callable[..., Awaitable[str | None]]) -> Callable[..., Awaitable[str | None]]:
    """Turn a crashing rail into a block.

    NeMo's action dispatcher logs an action exception and returns ``None``, which a Colang
    flow reads as "nothing found" — a rail that cannot run would wave the request through.
    Wrapping every check means an unevaluable rail blocks instead.
    """

    @functools.wraps(fn)
    async def guarded(*args: Any, **kwargs: Any) -> str | None:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any failure must block
            log.error("rail_action_failed", action=fn.__name__, error=str(exc))
            return f"rail_error|{fn.__name__}: {exc}"

    return guarded


SEVERITY = {
    "rail_error": "critical",
    "prompt_injection": "high",
    "control_bypass": "high",
    "unlicensed_advice": "medium",
    "financial_crime_facilitation": "critical",
    "tipping_off_request": "critical",
    "tipping_off": "critical",
    "sensitive_disclosure": "medium",
    "prohibited_credit_factor": "critical",
    "collections_threat": "critical",
    "collections_control_bypass": "critical",
}


def _text(context: dict[str, Any] | None, key: str) -> str:
    value = (context or {}).get(key)
    return value if isinstance(value, str) else ""


async def check_prompt_integrity(context: dict[str, Any] | None = None) -> str | None:
    """Prompt injection, and attempts to talk the agent past a platform control."""
    text = _text(context, "user_message")
    hit = first_match(PROMPT_INJECTION_PATTERNS, text)
    if hit:
        return f"prompt_injection|{hit}"
    hit = first_match(CONTROL_BYPASS_PATTERNS, text)
    if hit:
        return f"control_bypass|{hit}"
    return None


async def check_unlicensed_advice(context: dict[str, Any] | None = None) -> str | None:
    """A servicing agent answers on the customer's own products, not on what to buy."""
    hit = first_match(UNLICENSED_ADVICE_PATTERNS, _text(context, "user_message"))
    return f"unlicensed_advice|{hit}" if hit else None


async def check_financial_crime_request(context: dict[str, Any] | None = None) -> str | None:
    """Requests to help evade detection, reporting or sanctions — or to tip a subject off."""
    text = _text(context, "user_message")
    hit = first_match(EVASION_PATTERNS, text)
    if hit:
        return f"financial_crime_facilitation|{hit}"
    hit = first_match(TIPPING_OFF_PATTERNS, text)
    if hit:
        return f"tipping_off_request|{hit}"
    return None


async def check_tipping_off(context: dict[str, Any] | None = None) -> str | None:
    """Never tell a subject they are under investigation or that a report was filed.

    Tipping off is a criminal offence in most jurisdictions (PMLA s.63 in India,
    POCA s.333A in the UK), so this blocks rather than redacts.
    """
    hit = first_match(TIPPING_OFF_OUTPUT_PATTERNS, _text(context, "bot_message"))
    return f"tipping_off|{hit}" if hit else None


async def redact_sensitive_output(context: dict[str, Any] | None = None) -> str | None:
    """Mask unmasked identifiers in the answer.

    Returns the redacted text when something changed, otherwise ``None`` so the Colang
    flow leaves the message alone.
    """
    try:
        text = _text(context, "bot_message")
        allowlist = set((context or {}).get("pii_allowlist") or [])
        redacted = text
        for label, pattern in PII_PATTERNS:
            if label in allowlist:
                continue
            redacted = pattern.sub(lambda m: mask(m.group(0)), redacted)
        return redacted if redacted != text else None
    except Exception as exc:  # noqa: BLE001
        # Never substitute an error string for the response; the detector above has
        # already raised rail_error, which blocks the run.
        log.error("rail_redaction_failed", error=str(exc))
        return None


async def sensitive_labels(context: dict[str, Any] | None = None) -> str | None:
    """Which identifier classes were present, for the finding record."""
    text = _text(context, "bot_message")
    allowlist = set((context or {}).get("pii_allowlist") or [])
    found = [
        label
        for label, pattern in PII_PATTERNS
        if label not in allowlist and pattern.search(text)
    ]
    return f"sensitive_disclosure|{','.join(found)}" if found else None


async def check_prohibited_credit_factors(context: dict[str, Any] | None = None) -> str | None:
    """A credit decision may never turn on a protected characteristic.

    Checks both sides: a request to weigh one, and an answer that reasons from one.
    """
    for key in ("user_message", "bot_message"):
        hit = first_match(PROHIBITED_CREDIT_FACTORS, _text(context, key))
        if hit:
            return f"prohibited_credit_factor|{hit}"
    return None


async def check_collections_conduct(context: dict[str, Any] | None = None) -> str | None:
    """Threats, third-party disclosure and pressure tactics a collector may never use."""
    for key in ("user_message", "bot_message"):
        hit = first_match(COLLECTIONS_THREAT_PATTERNS, _text(context, key))
        if hit:
            return f"collections_threat|{hit}"
    return None


async def check_collections_control_bypass(context: dict[str, Any] | None = None) -> str | None:
    """Requests to contact someone despite a cease instruction, dispute or hardship plan."""
    hit = first_match(COLLECTIONS_BYPASS_PATTERNS, _text(context, "user_message"))
    return f"collections_control_bypass|{hit}" if hit else None


#: Detectors return a hit string or None. Every one is wrapped in `fail_closed` when it is
#: registered, so a detector added later cannot accidentally fail open.
DETECTORS = {
    "check_prompt_integrity": check_prompt_integrity,
    "check_unlicensed_advice": check_unlicensed_advice,
    "check_financial_crime_request": check_financial_crime_request,
    "check_tipping_off": check_tipping_off,
    "sensitive_labels": sensitive_labels,
    "check_prohibited_credit_factors": check_prohibited_credit_factors,
    "check_collections_conduct": check_collections_conduct,
    "check_collections_control_bypass": check_collections_control_bypass,
}

#: Transforms rewrite the message, so they must never return an error string as text and
#: are not wrapped. Their detector counterpart is what blocks.
TRANSFORMS = {
    "redact_sensitive_output": redact_sensitive_output,
}

#: Everything Colang can call by name.
ACTIONS = {**DETECTORS, **TRANSFORMS}
