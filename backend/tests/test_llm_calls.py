"""Rail verdicts, truncation, error redaction and readiness.

The boundary between a model's prose and the code that acts on it. A classifier is only
useful if its answer is read correctly, and NeMo reads only the first two words of one --
so a correct verdict phrased like a sentence becomes a refusal, and the operator is told
their request violated a policy it never touched.
"""

from __future__ import annotations

import pytest

from app.core.redaction import client_safe_error
from app.guardrails.llm_adapter import normalise_verdict
from app.llm.router import ModelRouter
from app.llm.types import LLMResponse, Usage


def _response(finish_reason: str) -> LLMResponse:
    return LLMResponse(content="x", model="m", provider="p", usage=Usage(), finish_reason=finish_reason)


class _Configured:
    """Stands in for a provider whose credential is set, independent of the environment."""

    def __init__(self, name: str):
        self.name = name
        self.configured = True


def _router_with_anthropic() -> ModelRouter:
    router = ModelRouter()
    router._providers = {"anthropic": _Configured("anthropic")}  # type: ignore[dict-item]
    return router


class TestRailVerdict:
    @pytest.mark.parametrize(
        "reply",
        [
            "no",
            "No.",
            "**No**",
            "  no  ",
            "Answer: no",
            "Based on the policy above, no. This is a legitimate operational request.",
            "The message is safe to process.",
            "no - assessing jurisdiction risk is ordinary due diligence",
        ],
    )
    def test_an_allow_verdict_is_read_as_allow(self, reply: str):
        assert normalise_verdict(reply) == "no"

    @pytest.mark.parametrize(
        "reply",
        [
            "yes",
            "Yes.",
            "Yes, this should be blocked.",
            "unsafe",
            "Based on the policy above, yes - this asks to bypass a required control.",
            "Answer: yes",
        ],
    )
    def test_a_block_verdict_is_read_as_block(self, reply: str):
        assert normalise_verdict(reply) == "yes"

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("This is not safe.", "yes"),
            ("The request is not permitted.", "yes"),
            ("There is no violation here.", "no"),
            ("No policy is breached.", "no"),
        ],
    )
    def test_negation_is_not_read_backwards(self, reply: str, expected: str):
        """Taking the bare token would invert these, which is the one error a rail must never make."""
        assert normalise_verdict(reply) == expected

    @pytest.mark.parametrize(
        "reply",
        ["", "   ", "I cannot determine that.", "Assessment complete. The records were retrieved."],
    )
    def test_a_reply_with_no_verdict_is_reported_as_such(self, reply: str):
        """None is not 'allow'. It says the classifier answered something else entirely."""
        assert normalise_verdict(reply) is None

    def test_the_first_verdict_in_the_reply_wins(self):
        assert normalise_verdict("No. There is no reason to block this.") == "no"


class TestTruncation:
    @pytest.mark.parametrize("reason", ["length", "max_tokens", "MAX_TOKENS"])
    def test_hitting_the_ceiling_is_reported(self, reason: str):
        assert _response(reason).truncated is True

    @pytest.mark.parametrize("reason", ["stop", "end_turn", "tool_calls", "tool_use"])
    def test_a_complete_reply_is_not_truncated(self, reason: str):
        assert _response(reason).truncated is False

    def test_truncation_reaches_the_serialised_form(self):
        """It has to survive into the trace, or nobody reading the run ever learns of it."""
        assert _response("length").to_dict()["truncated"] is True
        assert _response("stop").to_dict()["truncated"] is False


class TestClientSafeError:
    def test_the_actionable_message_survives(self):
        assert client_safe_error("Missing GOOGLE_API_KEY") == "Missing GOOGLE_API_KEY"

    def test_a_traceback_is_removed(self):
        message = 'Traceback (most recent call last):\n  File "/usr/lib/x/main.py", line 6\nValueError: x'
        assert "Traceback" not in client_safe_error(message)
        assert "main.py" not in client_safe_error(message)

    def test_an_absolute_path_is_replaced(self):
        result = client_safe_error("failed at /opt/app/venv/lib/httpx/_client.py handler")
        assert "/opt/app" not in result
        assert "[path]" in result

    def test_a_key_fragment_is_removed(self):
        assert "sk-abcd1234efgh5678ijkl" not in client_safe_error("bad key sk-abcd1234efgh5678ijkl")

    def test_stripping_everything_still_says_something_useful(self):
        assert client_safe_error('  File "/a/b/c.py", line 1') != ""
        assert "server log" in client_safe_error("")

    def test_internals_can_be_kept_for_local_debugging(self):
        raw = 'File "/usr/lib/x.py", line 3'
        assert client_safe_error(raw, reveal_internals=True) == raw

    def test_a_long_message_is_bounded(self):
        assert len(client_safe_error("boom " * 400)) <= 401


class TestReadiness:
    def test_no_credential_anywhere_names_the_variables(self):
        router = ModelRouter()
        router._providers = {}  # type: ignore[assignment]
        state = router.readiness()
        assert state["ready"] is False
        assert state["reason"] == "no provider credential is set"
        assert "ANTHROPIC_API_KEY" in state["set_one_of"]

    def test_a_usable_provider_reports_ready(self):
        router = _router_with_anthropic()
        state = router.readiness()
        assert state["ready"] is True
        assert state["usable"] == ["anthropic"]
        assert state["reason"] is None
        assert state["set_one_of"] == []

    def test_a_circuit_open_provider_is_reported_separately_from_a_missing_one(self):
        """These need different remedies: one is wait or investigate, the other is set a key."""
        from app.core.resilience import get_breaker

        router = _router_with_anthropic()
        breaker = get_breaker("llm:anthropic")
        breaker._state = "open"
        try:
            state = router.readiness()
            assert state["ready"] is False
            assert state["reason"] == "every configured provider is circuit-open"
            assert state["circuit_open"] == ["anthropic"]
            assert state["set_one_of"] == []  # nothing to set; the key is already there
        finally:
            breaker._state = "closed"
