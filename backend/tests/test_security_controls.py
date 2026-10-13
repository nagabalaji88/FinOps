"""Tests for the controls raised in the 2026-09-23 production readiness review.

Each class here covers a control that previously looked enabled and did nothing: a lockout
counter that was rolled back before it could ever reach its threshold, API-key scopes that
were stored and then ignored, an MFA factor any session could replace, a secret envelope
keyed off the signing key, an SSO callback that accepted any code, and a `writes_data` flag
the engine never consulted. They are regression tests first and documentation second --
each one fails against the revision the review examined.
"""

from __future__ import annotations

import base64

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import AuthError, RateLimitError
from app.core.rbac import Permission, permissions_from_scopes, unknown_scopes
from app.core.resilience import TokenBucketLimiter, limiter_for
from app.core.security import decrypt_value, encrypt_value
from app.db.models.identity import RefreshToken, User
from app.tools import registry
from app.tools.base import REVIEW_EXEMPT_WRITES


class TestFailedLoginsPersist:
    """The lockout counter has to survive the rejection that produced it.

    Login raises `AuthError` after incrementing, and the request session is rolled back on
    the way out, so the increment used to vanish with it -- leaving the five-attempt
    lockout permanently one attempt away from triggering.
    """

    async def test_a_failed_password_is_counted_after_the_request_is_rejected(
        self, client: AsyncClient, session
    ):
        email = "lockout.probe@finops.local"
        session.add(
            User(
                email=email,
                full_name="Lockout Probe",
                roles=["viewer"],
                hashed_password="$2b$12$" + "x" * 53,
            )
        )
        await session.commit()

        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "definitely-wrong"}
        )
        assert response.status_code == 401

        await session.rollback()
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_login_count == 1, "the increment was rolled back with the rejection"

    async def test_repeated_failures_lock_the_account(self, client: AsyncClient, session):
        email = "lockout.threshold@finops.local"
        session.add(
            User(
                email=email,
                full_name="Lockout Threshold",
                roles=["viewer"],
                hashed_password="$2b$12$" + "y" * 53,
            )
        )
        await session.commit()

        for _ in range(5):
            await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})

        await session.rollback()
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_login_count >= 5
        assert user.locked_until is not None

        # The lockout is what the caller is now told about, not "invalid credentials".
        response = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
        assert response.status_code == 403
        assert "locked" in response.json()["error"]["message"].lower()

    async def test_a_correct_password_is_never_throttled(self, client: AsyncClient):
        """Only failures spend the login budget, so lockout cannot be weaponised as a DoS."""
        for _ in range(12):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@finops.local", "password": "TestPassword!2026"},
            )
            assert response.status_code == 200


class TestApiKeyScopes:
    async def test_scopes_resolve_and_unknown_ones_are_named(self):
        assert permissions_from_scopes(["agent:execute"]) == {Permission.AGENT_EXECUTE}
        assert unknown_scopes(["agent:execute", "not:a:permission"]) == ["not:a:permission"]

    async def test_a_scoped_key_cannot_use_permissions_outside_its_scopes(
        self, client: AsyncClient, auth: dict
    ):
        created = await client.post(
            "/api/v1/auth/api-keys",
            headers=auth,
            json={"name": "scoped", "scopes": ["agent:read"], "rate_limit_per_minute": 500},
        )
        assert created.status_code == 201
        key = {"X-API-Key": created.json()["api_key"]}

        # Inside its scope.
        assert (await client.get("/api/v1/agents", headers=key)).status_code == 200
        # Its owner is an admin; the key is not, because the scope does not say so.
        assert (await client.get("/api/v1/auth/users", headers=key)).status_code == 403

        me = (await client.get("/api/v1/auth/me", headers=key)).json()
        assert me["permissions"] == ["agent:read"]

    async def test_an_unscoped_key_inherits_its_owner(self, client: AsyncClient, auth: dict):
        created = await client.post(
            "/api/v1/auth/api-keys",
            headers=auth,
            json={"name": "unscoped", "rate_limit_per_minute": 500},
        )
        key = {"X-API-Key": created.json()["api_key"]}
        assert (await client.get("/api/v1/auth/users", headers=key)).status_code == 200

    async def test_an_unknown_scope_is_refused_at_creation(self, client: AsyncClient, auth: dict):
        response = await client.post(
            "/api/v1/auth/api-keys",
            headers=auth,
            json={"name": "bad", "scopes": ["agent:execute", "everything"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"]["unknown"] == ["everything"]


class TestRateLimiterIsShared:
    def test_one_limiter_per_rate(self):
        """A limiter built per request hands every request a full bucket, i.e. no limit."""
        assert limiter_for(300) is limiter_for(300)
        assert limiter_for(300) is not limiter_for(120)

    async def test_a_custom_rate_actually_throttles(self):
        limiter = limiter_for(2)
        await limiter.enforce("custom-rate-probe")
        await limiter.enforce("custom-rate-probe")
        with pytest.raises(RateLimitError):
            await limiter.enforce("custom-rate-probe")

    async def test_the_local_bucket_map_is_bounded(self):
        limiter = TokenBucketLimiter(rate_per_minute=600)
        limiter.max_local_keys = 50
        for index in range(500):
            await limiter.enforce(f"caller-{index}")
        assert len(limiter._buckets) <= limiter.max_local_keys  # noqa: SLF001


MFA_USER_PASSWORD = "MfaProbePassword!2026"


@pytest.fixture
async def mfa_user(client: AsyncClient, auth: dict) -> dict[str, str]:
    """A throwaway account to enrol MFA on.

    The suite shares one database for the whole session, so enrolling a factor on the
    seeded administrator would leave every later test that signs in as admin demanding a
    TOTP code.
    """
    email = "mfa.probe@finops.local"
    await client.post(
        "/api/v1/auth/users",
        headers=auth,
        json={
            "email": email,
            "full_name": "MFA Probe",
            "password": MFA_USER_PASSWORD,
            "roles": ["viewer"],
        },
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": MFA_USER_PASSWORD})
    assert login.status_code == 200, login.text
    return {
        "email": email,
        "access": login.json()["access_token"],
        "refresh": login.json()["refresh_token"],
    }


class TestMfaStepUp:
    async def test_enrolment_requires_the_current_password(self, client: AsyncClient, mfa_user: dict):
        headers = {"Authorization": f"Bearer {mfa_user['access']}"}
        response = await client.post("/api/v1/auth/mfa/enrol", headers=headers, json={"password": "wrong"})
        assert response.status_code == 401

    async def test_the_live_factor_survives_an_abandoned_enrolment(
        self, client: AsyncClient, mfa_user: dict, session
    ):
        headers = {"Authorization": f"Bearer {mfa_user['access']}"}
        enrol = await client.post(
            "/api/v1/auth/mfa/enrol", headers=headers, json={"password": MFA_USER_PASSWORD}
        )
        assert enrol.status_code == 200
        assert enrol.json()["enrolment_token"], "the candidate secret travels in a signed ticket"

        await session.rollback()
        user = (await session.execute(select(User).where(User.email == mfa_user["email"]))).scalar_one()
        assert user.mfa_secret is None, "an abandoned enrolment must not overwrite the live factor"
        assert user.mfa_enabled is False

    async def test_a_ticket_issued_to_another_account_is_refused(
        self, client: AsyncClient, mfa_user: dict, auth: dict
    ):
        headers = {"Authorization": f"Bearer {mfa_user['access']}"}
        enrol = (
            await client.post("/api/v1/auth/mfa/enrol", headers=headers, json={"password": MFA_USER_PASSWORD})
        ).json()
        # The administrator presents the probe account's enrolment ticket.
        response = await client.post(
            "/api/v1/auth/mfa/verify",
            headers=auth,
            json={
                "code": pyotp.TOTP(enrol["secret"]).now(),
                "enrolment_token": enrol["enrolment_token"],
            },
        )
        assert response.status_code == 403

    async def test_enrolling_activates_the_factor_and_revokes_live_sessions(
        self, client: AsyncClient, mfa_user: dict, session
    ):
        headers = {"Authorization": f"Bearer {mfa_user['access']}"}
        enrol = (
            await client.post("/api/v1/auth/mfa/enrol", headers=headers, json={"password": MFA_USER_PASSWORD})
        ).json()
        verified = await client.post(
            "/api/v1/auth/mfa/verify",
            headers=headers,
            json={"code": pyotp.TOTP(enrol["secret"]).now(), "enrolment_token": enrol["enrolment_token"]},
        )
        assert verified.status_code == 200
        assert verified.json()["mfa_enabled"] is True

        # The refresh token minted before the factor changed is no longer usable.
        refused = await client.post("/api/v1/auth/refresh", json={"refresh_token": mfa_user["refresh"]})
        assert refused.status_code == 401

        await session.rollback()
        user = (await session.execute(select(User).where(User.email == mfa_user["email"]))).scalar_one()
        assert user.mfa_enabled is True
        assert (
            await session.execute(
                select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            )
        ).scalars().all() == []

        # Removing it needs the password *and* the factor currently in force.
        without_code = await client.request(
            "DELETE", "/api/v1/auth/mfa", headers=headers, json={"password": MFA_USER_PASSWORD}
        )
        assert without_code.status_code == 401
        removed = await client.request(
            "DELETE",
            "/api/v1/auth/mfa",
            headers=headers,
            json={"password": MFA_USER_PASSWORD, "mfa_code": pyotp.TOTP(user.mfa_secret).now()},
        )
        assert removed.status_code == 200
        assert removed.json()["mfa_enabled"] is False


class TestSsoTransactionBinding:
    async def test_the_callback_will_not_accept_a_code_without_a_state(self, client: AsyncClient):
        """`state` is what binds the callback to a browser that started the login here.

        Without it the endpoint exchanged whatever code it was handed, which is exactly
        the shape authorization-code injection needs: a code obtained elsewhere, posted
        here, redeemed for this platform's tokens.
        """
        response = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "stolen-code", "redirect_uri": "https://console.example.com/callback"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_error"
        missing = {tuple(e["loc"])[-1] for e in response.json()["error"]["details"]["errors"]}
        assert "state" in missing

    async def test_a_callback_for_an_unknown_state_is_refused(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/sso/callback",
            json={
                "code": "stolen-code",
                "state": "never-issued",
                "redirect_uri": "https://console.example.com/callback",
            },
        )
        # SSO is not configured in the suite, so this is refused before the exchange; what
        # matters is that no path issues tokens for an unbound code.
        assert response.status_code in {401, 422}
        assert "access_token" not in response.json()

    async def test_the_discovery_document_advertises_the_bound_flow(self, client: AsyncClient):
        info = (await client.get("/api/v1/auth/sso")).json()
        if info["configured"]:
            assert info["pkce"] == "S256"
            assert info["requires_verified_email"] is True
        else:
            assert "KEYCLOAK_URL" in info["required_settings"]


class TestSecretsAtRest:
    def test_round_trip_and_tamper_detection(self):
        sealed = encrypt_value("sk-provider-credential")
        assert sealed.startswith("v2."), "secrets should be sealed with the AEAD envelope"
        assert "sk-provider-credential" not in sealed
        assert decrypt_value(sealed) == "sk-provider-credential"

        # Flip one ciphertext byte: the GCM tag must refuse it rather than return plaintext.
        raw = bytearray(base64.urlsafe_b64decode(sealed.split(".", 1)[1]))
        raw[-1] ^= 0x01
        tampered = "v2." + base64.urlsafe_b64encode(bytes(raw)).decode()
        with pytest.raises(AuthError):
            decrypt_value(tampered)

    def test_the_envelope_is_not_keyed_off_the_signing_key_alone(self):
        """A separate key is the point: rotating JWT_SECRET must not strand stored secrets."""
        material, is_jwt_fallback = Settings(
            secret_encryption_key="a-dedicated-encryption-key-value"
        ).secret_encryption_material
        assert material == "a-dedicated-encryption-key-value"
        assert is_jwt_fallback is False
        assert Settings().secret_encryption_material[1] is True


class TestProductionRefusesInsecureDefaults:
    def test_the_shipped_defaults_are_all_reported(self):
        # Passed explicitly: init arguments outrank the environment, and the suite exports
        # its own JWT_SECRET and BOOTSTRAP_ADMIN_PASSWORD.
        shipped = Settings.model_fields
        problems = " ".join(
            Settings(
                environment="production",
                jwt_secret=shipped["jwt_secret"].default,
                secret_encryption_key=None,
                bootstrap_admin_password=shipped["bootstrap_admin_password"].default,
                seed_demo_users=True,
                trusted_hosts=["*"],
                cors_origins=["*"],
                forwarded_allow_ips="*",
            ).production_misconfigurations()
        )
        for expected in (
            "JWT_SECRET",
            "SECRET_ENCRYPTION_KEY",
            "BOOTSTRAP_ADMIN_PASSWORD",
            "SEED_DEMO_USERS",
            "TRUSTED_HOSTS",
            "CORS_ORIGINS",
            "FORWARDED_ALLOW_IPS",
        ):
            assert expected in problems

    def test_a_properly_configured_production_environment_is_accepted(self):
        settings = Settings(
            environment="production",
            jwt_secret="j" * 48,
            secret_encryption_key="e" * 48,
            bootstrap_admin_password="a-long-unique-admin-secret",
            seed_demo_users=False,
            trusted_hosts="console.example.com",
            cors_origins="https://console.example.com",
            forwarded_allow_ips="10.0.0.0/8",
        )
        assert settings.production_misconfigurations() == []
        # Docs and the metrics endpoint default closed in production.
        assert settings.api_docs_enabled is False
        assert settings.durable_artifact_store_required is True


class TestWriteToolsAreReviewed:
    def test_every_write_is_either_gated_or_exempt_with_a_reason(self):
        for tool in registry.all():
            if not tool.writes_data:
                continue
            reason = REVIEW_EXEMPT_WRITES.get(tool.name)
            assert tool.requires_human_review or reason, (
                f"'{tool.name}' writes data but is neither gated nor listed in "
                "REVIEW_EXEMPT_WRITES with a reason"
            )
            if reason is not None:
                assert len(reason) > 20, f"'{tool.name}' needs a real justification"

    def test_the_named_write_tools_from_the_review_are_now_gated(self):
        for name in (
            "create_support_ticket",
            "create_investigation_case",
            "scan_delinquent_accounts",
            "record_promise_to_pay",
            "open_payment_investigation",
        ):
            assert registry.get(name).requires_human_review, f"'{name}' still runs unreviewed"

    def test_exemptions_name_real_tools(self):
        for name in REVIEW_EXEMPT_WRITES:
            assert registry.has(name), f"REVIEW_EXEMPT_WRITES lists '{name}', which is not registered"
            assert registry.get(name).writes_data, f"'{name}' does not write; the exemption is noise"

    async def test_a_gated_write_cannot_be_reached_through_direct_invocation(
        self, client: AsyncClient, auth: dict
    ):
        """The engine gate is worth nothing if the tools endpoint runs the same tool."""
        response = await client.post(
            "/api/v1/tools/create_support_ticket/invoke",
            headers=auth,
            json={"arguments": {"customer_id": "CUS-100001", "subject": "x", "description": "y"}},
        )
        assert response.status_code == 422
        assert "approval" in response.json()["error"]["message"].lower()
