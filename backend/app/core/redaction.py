"""Trim provider error payloads before they reach a client.

A provider's error body is written for the account holder, not for the end user of an
application built on top of it. It routinely echoes the offending request back -- which for
this platform means customer data -- and embeds SDK internals, absolute paths and key
fragments. Passing it through verbatim leaks all three.

The goal is a redaction rather than a blank: "invalid x-api-key" is the difference between
an operator fixing the credential in a minute and an operator guessing for an hour.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Long opaque runs are the shape every provider's keys share (sk-..., AKIA..., JWTs).
_SECRET = re.compile(
    r"""(?xi)
    (sk-[A-Za-z0-9_\-]{6,})           # OpenAI / Anthropic style
    | (Bearer\s+\S+)                  # Authorization header echoed back
    | (AKIA[0-9A-Z]{12,})             # AWS access key id
    | (\b[A-Za-z0-9_\-]{40,}\b)       # bare high-entropy token
    """
)
_PATH = re.compile(r"(?:/[\w.\-]+){2,}\.py\b")
_MAX = 300


def _scrub(text: str) -> str:
    text = _SECRET.sub("[redacted]", text)
    text = _PATH.sub("[path]", text)
    return " ".join(text.split())


def _shorten(text: str, limit: int = _MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_TRACEBACK_HEADER = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)
_TRACEBACK_LINE = re.compile(r'\s*File "[^"]+", line \d+.*', re.MULTILINE)


def client_safe_error(message: str | None, *, reveal_internals: bool = False) -> str:
    """Strip an exception's internals while keeping the part that says what to do.

    A provider SDK embeds its own frames and absolute paths inside the exception *text*, so
    a message forwarded to an API client leaks the install layout and dependency versions
    without any traceback being formatted. The goal is a redaction and not a blank:
    "Missing GOOGLE_API_KEY" has to survive, or a diagnosable error becomes a shrug.
    """
    if reveal_internals:
        return message or ""
    cleaned = _TRACEBACK_HEADER.sub("", message or "")
    cleaned = _TRACEBACK_LINE.sub("", cleaned)
    cleaned = _scrub(cleaned)
    if not cleaned:
        return "The call failed. See the server log for details."
    return _shorten(cleaned, 400)


def redact_provider_body(body: str | bytes | None, *, limit: int = _MAX) -> str:
    """Reduce a provider's error response to the part an operator can act on.

    Providers agree on the shape ``{"error": {"message": ..., "type": ...}}`` closely enough
    that pulling those two fields keeps the diagnosis and drops the echoed request. Anything
    that does not parse falls back to a scrubbed, truncated excerpt.
    """
    if not body:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _shorten(_scrub(body), limit)
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, str):
        return _shorten(_scrub(error), limit)
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("type") or "").strip()
        kind = str(error.get("type") or error.get("code") or "").strip()
        if message:
            joined = f"{kind}: {message}" if kind and kind not in message else message
            return _shorten(_scrub(joined), limit)
    # Some gateways answer with a bare {"message": ...} or {"detail": ...}.
    if isinstance(parsed, dict):
        for key in ("message", "detail", "error_message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _shorten(_scrub(value), limit)
    return _shorten(_scrub(body), limit)
