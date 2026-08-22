"""The provider layer: error classification, redaction, JSON recovery and model selection.

These cover the boundary between the platform and a model provider -- the place where a
misclassified failure is expensive, because the resilience machinery reacts to a rejected
credential exactly as it would to an outage and reports the wrong one to the operator.
"""

from __future__ import annotations

import pytest

from app.core.errors import (
    AuthError,
    BudgetExceededError,
    CircuitOpenError,
    ProviderError,
    ProviderNotConfiguredError,
    is_transient,
)
from app.core.redaction import redact_provider_body
from app.core.resilience import CircuitBreaker, RetryPolicy, with_retry
from app.llm.jsonio import extract_json_object
from app.llm.router import PROVIDER_KEY_ENV_VARS, ModelRouter
from app.llm.types import LLMResponse, Message, Usage


class TestErrorClassification:
    @pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
    def test_a_transient_status_is_retryable(self, status: int):
        assert ProviderError("x", provider_status=status).retryable is True
        assert is_transient(ProviderError("x", provider_status=status)) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_a_client_status_is_not_retryable(self, status: int):
        assert ProviderError("x", provider_status=status).retryable is False
        assert is_transient(ProviderError("x", provider_status=status)) is False

    def test_a_transport_error_carries_no_status_and_stays_retryable(self):
        """A connection reset never reached the provider, so nothing has been ruled out."""
        error = ProviderError("connection reset")
        assert error.provider_status is None
        assert error.retryable is True

    def test_the_status_overrides_the_callers_default(self):
        assert ProviderError("x", provider_status=401, retryable=True).retryable is False

    def test_the_status_is_exposed_to_callers(self):
        assert ProviderError("x", provider_status=429).details["provider_status"] == 429

    @pytest.mark.parametrize(
        "exc",
        [
            ProviderNotConfiguredError("no key"),
            CircuitOpenError("open"),
            AuthError("bad token"),
            BudgetExceededError("spent"),
        ],
    )
    def test_permanent_failures_are_not_transient(self, exc: Exception):
        assert is_transient(exc) is False

    def test_an_unrecognised_failure_is_assumed_transient(self):
        """An outage this list does not name yet must still trip the breaker."""
        assert is_transient(RuntimeError("something new")) is True


class TestRetryLadder:
    async def test_a_rejected_credential_is_not_retried(self):
        """Three attempts at a 401 produce three identical rejections and a slower failure."""
        attempts = {"n": 0}

        async def unauthorised():
            attempts["n"] += 1
            raise ProviderError("401", provider_status=401)

        with pytest.raises(ProviderError):
            await with_retry(unauthorised, RetryPolicy(max_attempts=3, base_delay=0.01))
        assert attempts["n"] == 1

    async def test_an_outage_is_retried(self):
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ProviderError("503", provider_status=503)
            return "ok"

        assert await with_retry(flaky, RetryPolicy(max_attempts=3, base_delay=0.01)) == "ok"
        assert attempts["n"] == 3

    async def test_classification_can_be_switched_off(self):
        attempts = {"n": 0}

        async def unauthorised():
            attempts["n"] += 1
            raise ProviderError("401", provider_status=401)

        with pytest.raises(ProviderError):
            await with_retry(
                unauthorised, RetryPolicy(max_attempts=3, base_delay=0.01, only_if_transient=False)
            )
        assert attempts["n"] == 3


class TestBreakerCountsOnlyOutages:
    async def test_a_rejected_credential_never_opens_the_circuit(self):
        """Otherwise the message naming the fix is replaced by 'circuit is open' for everyone."""
        breaker = CircuitBreaker("creds", failure_threshold=2)

        async def unauthorised():
            raise ProviderError("401", provider_status=401)

        for _ in range(5):
            with pytest.raises(ProviderError):
                await breaker.call(unauthorised)
        assert breaker.state == "closed"

    async def test_an_outage_still_opens_the_circuit(self):
        breaker = CircuitBreaker("outage", failure_threshold=2)

        async def unavailable():
            raise ProviderError("503", provider_status=503)

        for _ in range(2):
            with pytest.raises(ProviderError):
                await breaker.call(unavailable)
        assert breaker.state == "open"

    async def test_a_rejected_credential_is_still_counted_for_reporting(self):
        breaker = CircuitBreaker("creds2", failure_threshold=2)

        async def unauthorised():
            raise ProviderError("401", provider_status=401)

        with pytest.raises(ProviderError):
            await breaker.call(unauthorised)
        assert breaker.snapshot()["total_failures"] == 1
        assert breaker.snapshot()["consecutive_failures"] == 0


class TestRedaction:
    def test_the_actionable_message_survives(self):
        body = '{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
        assert redact_provider_body(body) == "authentication_error: invalid x-api-key"

    def test_an_echoed_key_is_removed(self):
        body = '{"error":{"message":"Incorrect API key provided: sk-abcd1234efgh5678","type":"x"}}'
        result = redact_provider_body(body)
        assert "sk-abcd1234efgh5678" not in result
        assert "[redacted]" in result
        assert "Incorrect API key provided" in result

    def test_an_sdk_path_is_removed(self):
        assert "[path]" in redact_provider_body("File /usr/lib/python3/site-packages/openai/_base.py line 9")

    def test_an_echoed_request_body_does_not_survive_whole(self):
        """The echoed request is the customer data; only the diagnosis should come back."""
        body = (
            '{"error":{"type":"invalid_request_error","message":"max_tokens too large"},'
            '"request":{"messages":[{"role":"user","content":"account 12345678 for Priya Sharma"}]}}'
        )
        result = redact_provider_body(body)
        assert "Priya Sharma" not in result
        assert "max_tokens too large" in result

    def test_a_non_json_body_is_scrubbed_and_truncated(self):
        result = redact_provider_body("upstream connect error " * 100, limit=80)
        assert len(result) <= 80

    def test_an_empty_body_is_empty(self):
        assert redact_provider_body(None) == ""
        assert redact_provider_body("") == ""

    def test_bytes_are_accepted(self):
        assert "boom" in redact_provider_body(b'{"error":{"message":"boom"}}')


class TestJsonRecovery:
    def test_a_bare_object_parses(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_a_fenced_object_parses(self):
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_leading_prose_is_skipped(self):
        assert extract_json_object('Sure, here it is:\n{"a": 1}') == {"a": 1}

    def test_trailing_commentary_is_skipped(self):
        assert extract_json_object('{"a": 1}\n\nLet me know if you need more.') == {"a": 1}

    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        """The greedy-regex bug: it stops at the first '}', which is inside the value here."""
        text = 'Here:\n{"note": "closes with } here", "risk_level": "high"}'
        assert extract_json_object(text) == {"note": "closes with } here", "risk_level": "high"}

    def test_an_escaped_quote_does_not_end_the_string(self):
        assert extract_json_object(r'{"q": "she said \"stop\" }", "n": 2}') == {
            "q": 'she said "stop" }',
            "n": 2,
        }

    def test_a_nested_object_is_returned_whole(self):
        assert extract_json_object('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}

    def test_a_json_array_is_not_an_object(self):
        assert extract_json_object("[1, 2, 3]") is None

    def test_unparseable_text_yields_nothing(self):
        assert extract_json_object("I cannot answer that.") is None
        assert extract_json_object("") is None
        assert extract_json_object(None) is None


class _StubProvider:
    """Minimal provider double; the router only needs `configured` and `chat`."""

    def __init__(self, name: str, replies: list[str]):
        self.name = name
        self.configured = True
        self.replies = replies
        self.calls: list[list[Message]] = []

    async def chat(self, *, model: str, messages: list[Message], **kwargs):
        self.calls.append(list(messages))
        return LLMResponse(
            content=self.replies[min(len(self.calls) - 1, len(self.replies) - 1)],
            model=model,
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )


def _router_with(replies: list[str]) -> tuple[ModelRouter, _StubProvider]:
    router = ModelRouter()
    provider = _StubProvider("ollama", replies)
    router._providers = {"ollama": provider}  # type: ignore[dict-item]
    return router, provider


class TestChatJson:
    async def test_a_clean_reply_costs_one_call(self):
        router, provider = _router_with(['{"risk_level": "low"}'])
        parsed, response = await router.chat_json(
            messages=[Message(role="user", content="assess")], model="llama3.1"
        )
        assert parsed == {"risk_level": "low"}
        assert len(provider.calls) == 1
        assert response.usage.input_tokens == 10

    async def test_an_unparseable_reply_triggers_one_repair(self):
        router, provider = _router_with(["I think it is low risk.", '{"risk_level": "low"}'])
        parsed, _ = await router.chat_json(
            messages=[Message(role="user", content="assess")], model="llama3.1"
        )
        assert parsed == {"risk_level": "low"}
        assert len(provider.calls) == 2
        assert "not valid JSON" in provider.calls[1][-1].content

    async def test_the_repair_is_attempted_only_once(self):
        router, provider = _router_with(["still prose", "more prose"])
        parsed, _ = await router.chat_json(
            messages=[Message(role="user", content="assess")], model="llama3.1"
        )
        assert parsed is None
        assert len(provider.calls) == 2

    async def test_both_calls_are_billed(self):
        """Charging only the repair would under-report what the run actually spent."""
        router, _ = _router_with(["prose", '{"ok": true}'])
        _, response = await router.chat_json(
            messages=[Message(role="user", content="assess")], model="llama3.1"
        )
        assert response.usage.input_tokens == 20
        assert response.usage.output_tokens == 10


@pytest.fixture
def two_provider_router() -> ModelRouter:
    """Anthropic and OpenAI stubbed as configured, so selection does not depend on the env."""
    router = ModelRouter()
    router._providers = {  # type: ignore[assignment]
        "anthropic": _StubProvider("anthropic", ["{}"]),
        "openai": _StubProvider("openai", ["{}"]),
    }
    return router


class TestProviderSelection:
    def test_every_provider_names_its_credential(self):
        """A provider added without an entry here reports 'unavailable' and nothing else."""
        assert set(PROVIDER_KEY_ENV_VARS) == set(ModelRouter()._providers)

    def test_selection_skips_a_circuit_open_provider(self, two_provider_router: ModelRouter):
        from app.core.resilience import get_breaker

        breaker = get_breaker("llm:anthropic")
        breaker._state = "open"
        try:
            assert "anthropic" not in two_provider_router.usable_providers()
            assert two_provider_router.select().provider == "openai"
            assert all(
                m.provider != "anthropic" for m in two_provider_router.available_models(usable_only=True)
            )
        finally:
            breaker._state = "closed"

    def test_a_fallback_chain_excludes_a_circuit_open_provider(self, two_provider_router: ModelRouter):
        """A provider that is already failing is not somewhere to fall back to."""
        from app.core.resilience import get_breaker

        spec = two_provider_router.select(model="claude-sonnet-4-5")
        breaker = get_breaker("llm:openai")
        breaker._state = "open"
        try:
            chain = two_provider_router.fallback_chain(spec, needs_tools=False)
            assert all(m.provider != "openai" for m in chain)
        finally:
            breaker._state = "closed"

    def test_the_error_names_the_variable_to_set(self):
        router = ModelRouter()
        router._providers = {}  # type: ignore[assignment]
        with pytest.raises(ProviderNotConfiguredError) as caught:
            router.select(model="claude-sonnet-4-5")
        details = caught.value.details
        assert details["required_for_requested_model"] == "ANTHROPIC_API_KEY"
        assert "OPENAI_API_KEY" in details["set_one_of"]

    def test_a_configured_provider_is_still_offered_when_every_breaker_is_open(
        self, two_provider_router: ModelRouter
    ):
        """Otherwise the operator is told to set a key that is already set."""
        from app.core.resilience import get_breaker

        breakers = [get_breaker("llm:anthropic"), get_breaker("llm:openai")]
        for breaker in breakers:
            breaker._state = "open"
        try:
            assert two_provider_router.usable_providers() == []
            assert two_provider_router.select() is not None
        finally:
            for breaker in breakers:
                breaker._state = "closed"
