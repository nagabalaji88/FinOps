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
    CONTROL_BYPASS_PATTERNS,
    EVASION_PATTERNS,
    PII_PATTERNS,
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


#: Detectors return a hit string or None. Every one is wrapped in `fail_closed` when it is
#: registered, so a detector added later cannot accidentally fail open.
DETECTORS = {
    "check_prompt_integrity": check_prompt_integrity,
    "check_unlicensed_advice": check_unlicensed_advice,
    "check_financial_crime_request": check_financial_crime_request,
    "check_tipping_off": check_tipping_off,
    "sensitive_labels": sensitive_labels,
}

#: Transforms rewrite the message, so they must never return an error string as text and
#: are not wrapped. Their detector counterpart is what blocks.
TRANSFORMS = {
    "redact_sensitive_output": redact_sensitive_output,
}

#: Everything Colang can call by name.
ACTIONS = {**DETECTORS, **TRANSFORMS}
