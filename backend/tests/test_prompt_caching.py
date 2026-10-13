"""Prompt caching: the stable prefix of a request must be marked, and must stay stable.

An agent re-sends the same system prompt and the same tool schemas on every iteration of
its reasoning loop. That prefix is the bulk of the input tokens. The cost ledger has priced
a cache read since the catalogue was written and the provider has read `cache_read_input_tokens`
back off the response for just as long -- but nothing ever asked for the cache, so the
saving was measured and never taken.
"""

from __future__ import annotations

import json

import pytest

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.types import Message, ToolSchema

TOOLS = [
    ToolSchema(
        name="get_account_balance", description="Balances for a customer.", parameters={"type": "object"}
    ),
    ToolSchema(name="search_faq", description="Search the FAQ.", parameters={"type": "object"}),
]
SYSTEM = Message(role="system", content="You are a regulated financial-services agent.")


def _payload(provider: AnthropicProvider, messages, tools=TOOLS):
    return provider._payload("claude-haiku-4-5", messages, tools, 0.2, 2048, None, None)  # noqa: SLF001


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider()


class TestCacheBreakpoints:
    def test_the_tool_array_and_system_prompt_are_marked(self, provider):
        payload = _payload(provider, [SYSTEM, Message(role="user", content="What is my balance?")])
        from app.llm.providers.anthropic import _cacheable

        _cacheable(payload)

        # Anthropic caches up to a breakpoint, so the marker belongs on the last element of
        # each stable section -- not on every one.
        assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in payload["tools"][0]
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_a_request_with_no_tools_still_caches_the_system_prompt(self, provider):
        from app.llm.providers.anthropic import _cacheable

        payload = _payload(provider, [SYSTEM, Message(role="user", content="Hello")], tools=None)
        _cacheable(payload)

        assert "tools" not in payload
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_marking_an_empty_payload_is_a_no_op(self, provider):
        from app.llm.providers.anthropic import _cacheable

        payload = _payload(provider, [Message(role="user", content="Hello")], tools=None)
        _cacheable(payload)  # no system, no tools

        assert "system" not in payload
        assert "tools" not in payload


class TestThePrefixStaysStable:
    """The whole mechanism rests on the prefix being byte-identical between calls.

    A timestamp, a per-request id or a stray newline anywhere before the breakpoint drops
    the hit rate to zero, and nothing downstream would report that -- the bill would just
    stay the same. This is the property worth pinning.
    """

    def test_the_cached_prefix_is_identical_as_the_conversation_grows(self, provider):
        from app.llm.providers.anthropic import _cacheable

        turn_one = _payload(provider, [SYSTEM, Message(role="user", content="What is my balance?")])
        turn_two = _payload(
            provider,
            [
                SYSTEM,
                Message(role="user", content="What is my balance?"),
                Message(role="assistant", content="Which account?"),
                Message(role="user", content="The savings one."),
            ],
        )
        _cacheable(turn_one)
        _cacheable(turn_two)

        # Everything up to the breakpoint must match byte for byte...
        assert json.dumps(turn_one["tools"]) == json.dumps(turn_two["tools"])
        assert json.dumps(turn_one["system"]) == json.dumps(turn_two["system"])
        # ...while the part after it is what actually differs between the two calls.
        assert turn_one["messages"] != turn_two["messages"]


class TestJsonModeAndTheToggle:
    def test_json_mode_appends_a_block_and_the_breakpoint_follows_it(self, provider):
        """json_mode used to concatenate onto a string system prompt.

        With the system prompt in block form that would have raised, and a breakpoint
        placed before the appended instruction would have cached a prefix the request no
        longer sends.
        """
        from app.llm.providers.anthropic import _cacheable

        payload = _payload(provider, [SYSTEM, Message(role="user", content="Hi")], tools=None)
        payload.setdefault("system", []).append(
            {"type": "text", "text": "Respond with a single valid JSON object and nothing else."}
        )
        _cacheable(payload)

        assert len(payload["system"]) == 2
        assert "JSON" in payload["system"][-1]["text"]
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in payload["system"][0]

    async def test_the_setting_governs_whether_caching_is_requested(self, provider, monkeypatch):
        from app.core.config import settings

        captured: dict = {}

        class _Resp:
            status_code = 200
            headers: dict = {}

            @staticmethod
            def json():
                return {
                    "content": [{"type": "text", "text": "ok"}],
                    "model": "claude-haiku-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 8},
                }

        class _Client:
            async def post(self, _url, **kwargs):
                captured.update(kwargs.get("json") or {})
                return _Resp()

        monkeypatch.setattr("app.llm.providers.anthropic.http_client", lambda: _Client())
        monkeypatch.setattr(AnthropicProvider, "api_key", property(lambda self: "test-key"))

        monkeypatch.setattr(settings, "prompt_caching_enabled", False)
        await provider.chat(
            model="claude-haiku-4-5", messages=[SYSTEM, Message(role="user", content="Hi")], tools=TOOLS
        )
        assert "cache_control" not in json.dumps(captured)

        captured.clear()
        monkeypatch.setattr(settings, "prompt_caching_enabled", True)
        response = await provider.chat(
            model="claude-haiku-4-5", messages=[SYSTEM, Message(role="user", content="Hi")], tools=TOOLS
        )
        assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        # The hit the provider reports is what the ledger prices.
        assert response.usage.cached_input_tokens == 8


class TestTheLedgerPricesACacheRead:
    def test_a_cache_hit_costs_less_than_a_miss(self):
        from app.llm.catalog import compute_cost, resolve_model

        spec = resolve_model("claude-haiku-4-5")
        assert spec.cached_input_price_per_mtok > 0, "catalogue must price a cache read"

        miss = compute_cost(spec, input_tokens=100_000, output_tokens=1_000, cached=0)
        hit = compute_cost(spec, input_tokens=100_000, output_tokens=1_000, cached=90_000)
        assert hit < miss


class TestJoseAlgorithmConfusionMitigation:
    """CVE-2026-85394 has no upstream fix, so the mitigation is ours to hold.

    python-jose through 3.5.0 (the latest release) accepts a DER-encoded public key as an
    HMAC key, so an attacker holding the service's public key can forge an HS256 token that
    verifies -- but only where the verifier has not restricted the accepted algorithms.
    Both of our verifiers do, which is why the advisory is ignored in CI rather than
    blocking it. These tests are what make that ignore safe to keep: widen either allowlist
    and the build fails instead of quietly becoming exploitable.
    """

    def test_the_sso_verifier_never_accepts_a_symmetric_algorithm(self):
        import inspect

        from app.api.v1 import auth

        source = inspect.getsource(auth._verified_id_token)  # noqa: SLF001
        assert "algorithms=" in source
        # The forgery is an HS* token presented where an RS*/ES* one is expected.
        for symmetric in ("HS256", "HS384", "HS512", "none"):
            assert f'"{symmetric}"' not in source, (
                f"{symmetric} must not be accepted when verifying an SSO identity token; "
                "that is the exact shape CVE-2026-85394 exploits"
            )

    def test_the_platforms_own_tokens_pin_a_single_algorithm(self):
        from app.core.config import settings
        from app.core.security import create_access_token, decode_token

        # Symmetric and keyed on a secret the caller never supplies, so the DER-as-HMAC
        # trick has no way in -- but the algorithm is still pinned rather than inferred.
        assert settings.jwt_algorithm == "HS256"
        token = create_access_token(user_id="u1", email="a@b.local", roles=["viewer"], scopes=[])
        assert decode_token(token)["sub"] == "u1"
