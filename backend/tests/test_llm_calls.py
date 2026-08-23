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
from app.llm.types import LLMResponse, Message, Usage


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


class _Rejecting:
    """A provider whose account is entitled to only some of the catalogued models."""

    name = "openai"
    configured = True

    def __init__(self, allowed: set[str] | None = None):
        self.allowed = allowed or set()
        self.calls: list[str] = []

    async def chat(self, *, model: str, **_: object) -> LLMResponse:
        from app.core.errors import ProviderError

        self.calls.append(model)
        if model in self.allowed:
            return LLMResponse(content="ok", model=model, provider="openai", usage=Usage())
        raise ProviderError(
            f"openai returned 404: invalid_request_error: The model `{model}` does not exist "
            f"or you do not have access to it.",
            provider_status=404,
        )


def _router_with(provider: _Rejecting) -> ModelRouter:
    router = ModelRouter()
    router._providers = {"openai": provider}  # type: ignore[dict-item]
    return router


class TestInaccessibleModels:
    """A catalogue is a price list, not an entitlement list. Only the provider knows which
    of its models a given key may call, and it only says so by rejecting one."""

    @pytest.mark.asyncio
    async def test_it_reaches_the_one_model_the_account_can_call(self):
        provider = _Rejecting(allowed={"gpt-4o"})
        router = _router_with(provider)
        result = await router.chat(messages=[Message(role="user", content="hi")])
        assert result.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_a_rejected_model_is_not_tried_again(self):
        """Otherwise one inaccessible entry costs a wasted round trip on every single call."""
        provider = _Rejecting(allowed={"gpt-4o"})
        router = _router_with(provider)
        await router.chat(messages=[Message(role="user", content="hi")])
        provider.calls.clear()
        await router.chat(messages=[Message(role="user", content="hi")])
        assert provider.calls == ["gpt-4o"]

    @pytest.mark.asyncio
    async def test_the_error_names_every_model_tried(self):
        """Reporting only the last one describes a fallback the caller never asked for."""
        from app.core.errors import ProviderError

        provider = _Rejecting()
        router = _router_with(provider)
        with pytest.raises(ProviderError) as caught:
            await router.chat(messages=[Message(role="user", content="hi")])
        message = str(caught.value)
        assert "gpt-4o-mini" in message and "gpt-5.5-mini" in message
        assert len(caught.value.details["attempted"]) >= 2

    @pytest.mark.asyncio
    async def test_exhausting_the_catalogue_says_so_rather_than_blaming_credentials(self):
        from app.core.errors import ProviderNotConfiguredError

        provider = _Rejecting()
        router = _router_with(provider)
        for _ in range(6):
            try:
                await router.chat(messages=[Message(role="user", content="hi")])
            except ProviderNotConfiguredError as exc:
                assert "rejected by its provider" in exc.message
                assert exc.details["rejected"]
                assert "DEFAULT_MODEL" in exc.details["hint"]
                return
            except Exception:
                continue
        pytest.fail("never settled on the exhausted-catalogue error")

    @pytest.mark.asyncio
    async def test_one_call_is_bounded(self):
        """An account entitled to nothing must fail quickly, not walk the whole catalogue."""
        from app.core.errors import AppError
        from app.llm.router import MAX_CANDIDATES

        provider = _Rejecting()
        router = _router_with(provider)
        with pytest.raises(AppError):
            await router.chat(messages=[Message(role="user", content="hi")])
        assert len(provider.calls) <= MAX_CANDIDATES

    def test_an_ordinary_failure_does_not_demote_a_model(self):
        """A 500 or a rate limit is the provider having a bad minute, not a missing entitlement."""
        from app.core.errors import ProviderError
        from app.llm.router import _is_model_unavailable

        assert not _is_model_unavailable(ProviderError("boom", provider_status=500))
        assert not _is_model_unavailable(ProviderError("slow down", provider_status=429))
        assert not _is_model_unavailable(ProviderError("bad request", provider_status=400))
        assert _is_model_unavailable(ProviderError("The model `x` does not exist", provider_status=404))

    def test_a_demotion_can_be_lifted_without_a_restart(self):
        router = _router_with(_Rejecting())
        router.demote("gpt-4o", reason="test")
        assert "gpt-4o" in router.unavailable_models
        router.restore("gpt-4o")
        assert "gpt-4o" not in router.unavailable_models
