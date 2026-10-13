"""Recovering a JSON object from a reply that is not only JSON."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """Pull the first complete JSON object out of a model reply.

    Models wrap the object in a fenced block, preface it with a sentence of agreement, or
    append a note after it, in any combination. The scan tracks string state so a brace
    inside a value -- ``{"note": "see } below"}`` -- does not end the object early, which a
    plain ``\\{.*\\}`` match gets wrong in exactly the cases that carry customer text.
    """
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip())
    try:
        whole = json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    else:
        return whole if isinstance(whole, dict) else None

    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        value = json.loads(cleaned[start : index + 1])
                    except json.JSONDecodeError:
                        start = None  # Not valid on its own; keep looking for the next one.
                    else:
                        if isinstance(value, dict):
                            return value
                        start = None
    return None
