"""Unit tests for security, resilience, RBAC, chunking, retrieval and domain algorithms."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import CircuitOpenError, RateLimitError
from app.core.rbac import Permission, has_permission, permissions_for_roles
from app.core.resilience import CircuitBreaker, RetryPolicy, TokenBucketLimiter, with_retry
from app.core.security import (
    create_access_token,
    decrypt_value,
    encrypt_value,
    generate_api_key,
    hash_api_key,
    hash_password,
    mask_secret,
    new_totp_secret,
    verify_password,
    verify_totp,
)
from app.llm.catalog import compute_cost, resolve_model
from app.rag.chunking import chunk_text
from app.rag.embeddings import local_embed
from app.tools.kyc import mrz_check_digit, name_similarity, normalise_name, verhoeff_valid


class TestSecurity:
    def test_password_round_trip(self):
        hashed = hash_password("Correct Horse Battery Staple!")
        assert verify_password("Correct Horse Battery Staple!", hashed)
        assert not verify_password("wrong", hashed)

    def test_password_longer_than_bcrypt_limit(self):
        long_password = "x" * 200
        hashed = hash_password(long_password)
        assert verify_password(long_password, hashed)
        assert not verify_password("x" * 199, hashed)

    def test_jwt_round_trip(self):
        from app.core.security import decode_token

        token = create_access_token(user_id="u1", email="a@b.co", roles=["admin"], scopes=[])
        claims = decode_token(token)
        assert claims["sub"] == "u1"
        assert claims["roles"] == ["admin"]

    def test_jwt_rejects_wrong_type(self):
        from app.core.errors import AuthError
        from app.core.security import create_refresh_token, decode_token

        refresh = create_refresh_token(user_id="u1")
        with pytest.raises(AuthError):
            decode_token(refresh, expected_type="access")

    def test_api_key_hashing(self):
        full, prefix, hashed = generate_api_key()
        assert full.startswith("fops_")
        assert prefix in full
        assert hash_api_key(full) == hashed

    def test_secret_envelope(self):
        secret = "sk-super-secret-value"
        sealed = encrypt_value(secret)
        assert secret not in sealed
        assert decrypt_value(sealed) == secret

    def test_mask_secret(self):
        assert mask_secret("abcdefghij").startswith("abcd")
        assert "efghij" not in mask_secret("abcdefghij")

    def test_totp(self):
        import pyotp

        secret = new_totp_secret()
        assert verify_totp(secret, pyotp.TOTP(secret).now())
        assert not verify_totp(secret, "000000")


class TestRbac:
    def test_admin_has_every_permission(self):
        assert len(permissions_for_roles(["admin"])) == len(list(Permission))

    def test_viewer_cannot_execute(self):
        assert not has_permission(["viewer"], Permission.AGENT_EXECUTE)
        assert has_permission(["viewer"], Permission.AGENT_READ)

    def test_approver_can_decide(self):
        assert has_permission(["approver"], Permission.APPROVAL_DECIDE)
        assert not has_permission(["operator"], Permission.APPROVAL_DECIDE)

    def test_roles_compose(self):
        perms = permissions_for_roles(["viewer", "approver"])
        assert Permission.APPROVAL_DECIDE in perms


class TestResilience:
    async def test_retry_eventually_succeeds(self):
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        result = await with_retry(flaky, RetryPolicy(max_attempts=4, base_delay=0.01))
        assert result == "ok"
        assert attempts["n"] == 3

    async def test_retry_gives_up_on_excluded_error(self):
        async def fails():
            raise ValueError("permanent")

        with pytest.raises(ValueError):
            await with_retry(fails, RetryPolicy(max_attempts=3, base_delay=0.01,
                                                give_up_on=(ValueError,)))

    async def test_circuit_breaker_opens_and_recovers(self):
        breaker = CircuitBreaker("test", failure_threshold=2, recovery_seconds=0.2)

        async def failing():
            raise RuntimeError("down")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await breaker.call(failing)
        assert breaker.state == "open"
        with pytest.raises(CircuitOpenError):
            await breaker.call(failing)

        await asyncio.sleep(0.25)

        async def working():
            return "recovered"

        assert await breaker.call(working) == "recovered"
        assert breaker.state == "closed"

    async def test_rate_limiter(self):
        limiter = TokenBucketLimiter(rate_per_minute=60, burst=2)
        await limiter.enforce("client")
        await limiter.enforce("client")
        with pytest.raises(RateLimitError):
            await limiter.enforce("client")


class TestPricing:
    def test_resolve_alias(self):
        assert resolve_model("claude").id == "claude-sonnet-4-5"

    def test_cost_computation(self):
        spec = resolve_model("claude-sonnet-4-5")
        cost = compute_cost(spec, 1_000_000, 1_000_000)
        assert cost == pytest.approx(spec.input_price_per_mtok + spec.output_price_per_mtok)

    def test_cached_tokens_are_discounted(self):
        spec = resolve_model("claude-sonnet-4-5")
        full = compute_cost(spec, 1_000_000, 0, 0)
        cached = compute_cost(spec, 1_000_000, 0, 1_000_000)
        assert cached < full


class TestChunking:
    def test_headings_are_preserved(self):
        text = "# Alpha\n" + ("Alpha body. " * 60) + "\n# Beta\n" + ("Beta body. " * 60)
        chunks = chunk_text(text, max_tokens=64, overlap_tokens=8)
        assert len(chunks) > 2
        assert {"Alpha", "Beta"} <= {c.heading for c in chunks}

    def test_short_text_single_chunk(self):
        chunks = chunk_text("A short policy statement.")
        assert len(chunks) == 1

    def test_empty_text(self):
        assert chunk_text("") == []


class TestEmbeddings:
    def test_local_embeddings_are_deterministic_and_normalised(self):
        a, b = local_embed(["capital adequacy ratio"] * 2)
        assert a == b
        magnitude = sum(x * x for x in a) ** 0.5
        assert magnitude == pytest.approx(1.0, abs=1e-5)

    def test_similar_text_scores_higher(self):
        import numpy as np

        vectors = local_embed([
            "sanctions screening policy for onboarding",
            "sanctions screening during customer onboarding",
            "credit card reward points redemption",
        ])
        v = [np.array(x) for x in vectors]
        assert float(v[0] @ v[1]) > float(v[0] @ v[2])


class TestIdentityAlgorithms:
    def test_verhoeff_checksum(self):
        # 234123412346 is a documented valid Verhoeff sequence.
        assert verhoeff_valid("234123412346")
        assert not verhoeff_valid("234123412345")
        assert not verhoeff_valid("12345")

    def test_mrz_check_digit(self):
        # Documented ICAO 9303 examples.
        assert mrz_check_digit("L898902C3") == 6
        assert mrz_check_digit("740812") == 2
        assert mrz_check_digit("120415") == 9

    def test_name_normalisation_and_similarity(self):
        assert normalise_name("José  Márquez-Peña") == "jose marquez pena"
        assert name_similarity("Viktor Petrovich Sokolov", "Viktor Petrovich Sokolov") == 1.0
        assert name_similarity("Rajesh Kumar", "Priya Sharma") < 0.4


class TestSettingsParsing:
    """`CORS_ORIGINS` is the one setting every deployment edits by hand.

    pydantic-settings JSON-decodes complex types straight from the environment before any
    validator runs, so a comma-separated value — the form Compose, Helm and every `.env`
    write — used to fail at import with an opaque SettingsError, taking down `init-db`
    before it could create a single table. These cases pin every form we accept.
    """

    @staticmethod
    def _origins(raw: str | None) -> list[str]:
        import os

        from app.core.config import Settings

        previous = os.environ.get("CORS_ORIGINS")
        if raw is None:
            os.environ.pop("CORS_ORIGINS", None)
        else:
            os.environ["CORS_ORIGINS"] = raw
        try:
            return Settings().cors_origins
        finally:
            if previous is None:
                os.environ.pop("CORS_ORIGINS", None)
            else:
                os.environ["CORS_ORIGINS"] = previous

    def test_comma_separated_is_the_documented_form(self):
        assert self._origins("https://execute.example.com,https://console.example.com") == [
            "https://execute.example.com",
            "https://console.example.com",
        ]

    def test_whitespace_and_trailing_separators_are_tolerated(self):
        assert self._origins(" https://a.example , https://b.example , ") == [
            "https://a.example",
            "https://b.example",
        ]

    def test_json_arrays_still_work(self):
        assert self._origins('["https://a.example", "https://b.example"]') == [
            "https://a.example",
            "https://b.example",
        ]

    def test_a_single_origin_is_a_list_of_one(self):
        assert self._origins("https://only.example") == ["https://only.example"]

    def test_empty_means_no_origins_not_a_crash(self):
        assert self._origins("") == []

    def test_the_default_covers_both_dev_servers(self):
        origins = self._origins(None)
        assert "http://localhost:5173" in origins   # platform console
        assert "http://localhost:5174" in origins   # execute console

    def test_malformed_json_explains_itself(self):
        import pytest

        with pytest.raises(Exception) as caught:
            self._origins('["https://a.example",')
        assert "comma-separated" in str(caught.value)
