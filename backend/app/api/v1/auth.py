"""Authentication: password login, MFA, refresh, API keys, SSO metadata."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, PrincipalDep, SessionDep, write_audit
from app.core.cache import cache
from app.core.config import settings
from app.core.errors import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from app.core.rbac import ROLE_DESCRIPTIONS, ROLE_PERMISSIONS, Permission, unknown_scopes
from app.core.resilience import TokenBucketLimiter
from app.core.security import (
    MFA_ENROL,
    MFA_ENROL_TTL_SECONDS,
    create_access_token,
    create_mfa_enrolment_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_password,
    new_totp_secret,
    totp_provisioning_uri,
    verify_password,
    verify_totp,
)
from app.db.models.identity import ApiKey, RefreshToken, User

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _validate_email(value: str) -> str:
    """Accept internal domains (e.g. *.local) that strict deliverability checks reject."""
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValueError("value is not a valid email address")
    return value


MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

#: Password guessing is throttled per source-and-account. Only failures spend budget, so a
#: caller who knows the password is never told to come back later, and an attacker who does
#: not gets `login_failure_limit_per_minute` guesses a minute regardless of how many
#: accounts the lockout counter happens to be tracking.
_login_limiter = TokenBucketLimiter(settings.login_failure_limit_per_minute)


def _login_throttle_key(request: Request, email: str) -> str:
    source = request.client.host if request.client else "unknown"
    return f"login:{source}:{email}"


async def _guard_login_rate(request: Request, email: str) -> None:
    # Cost 0 reads the bucket without spending it; the spend happens in
    # `_record_login_failure` so that a correct password is never rate limited.
    _, remaining = await _login_limiter.check(_login_throttle_key(request, email), cost=0.0)
    if remaining < 1.0:
        raise RateLimitError(
            "Too many failed sign-in attempts. Try again shortly.",
            details={"retry_after_seconds": round(1.0 / _login_limiter.rate, 2)},
        )


async def _record_login_failure(*, user_id: str | None, email: str, reason: str, request: Request) -> None:
    """Persist a failed sign-in in its own transaction, then let the caller reject.

    The request's session is rolled back when the endpoint raises, so an increment written
    there disappears together with the rejection: the lockout counter would sit at zero
    forever and no failure would ever reach the audit trail. This commits independently.
    """
    from app.db.session import session_scope

    await _login_limiter.check(_login_throttle_key(request, email), cost=1.0)
    details: dict[str, Any] = {"reason": reason}
    async with session_scope() as failures:
        if user_id is not None:
            user = (await failures.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if user is not None:
                user.failed_login_count += 1
                details["attempts"] = user.failed_login_count
                if user.failed_login_count >= MAX_FAILED_LOGINS:
                    user.locked_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_MINUTES)
                    details["locked_until"] = user.locked_until.isoformat()
        await write_audit(
            failures,
            principal=None,
            action="auth.login" if reason != "bad_mfa_code" else "auth.mfa",
            resource_type="user",
            resource_id=user_id or email,
            outcome="failure",
            severity="warning",
            details=details,
            request=request,
        )


class LoginRequest(BaseModel):
    email: str
    password: str
    mfa_code: str | None = None

    _normalise_email = field_validator("email")(lambda cls, v: _validate_email(v))


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    await _guard_login_rate(request, payload.email)
    user = (
        await session.execute(select(User).where(func.lower(User.email) == payload.email.lower()))
    ).scalar_one_or_none()
    if user is None or not user.hashed_password:
        await _record_login_failure(user_id=None, email=payload.email, reason="unknown_user", request=request)
        raise AuthError("Invalid credentials")
    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise ForbiddenError(
            "Account temporarily locked", details={"locked_until": user.locked_until.isoformat()}
        )
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    if not verify_password(payload.password, user.hashed_password):
        await _record_login_failure(
            user_id=user.id, email=payload.email, reason="bad_password", request=request
        )
        raise AuthError("Invalid credentials")
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise AuthError("MFA code required", details={"mfa_required": True})
        if not verify_totp(user.mfa_secret or "", payload.mfa_code):
            await _record_login_failure(
                user_id=user.id, email=payload.email, reason="bad_mfa_code", request=request
            )
            raise AuthError("Invalid MFA code")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)

    access = create_access_token(
        user_id=user.id,
        email=user.email,
        roles=list(user.roles or []),
        scopes=sorted(str(p) for p in ROLE_PERMISSIONS.get("viewer", set())),
    )
    refresh = create_refresh_token(user_id=user.id)
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=decode_token(refresh, expected_type="refresh")["jti"],
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    )
    await write_audit(
        session,
        principal=None,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
        details={"method": "password"},
        request=request,
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=Principal(user, auth_method="jwt").to_dict(),
    )


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(payload: RefreshRequest, session: SessionDep) -> TokenResponse:
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    stored = (
        await session.execute(select(RefreshToken).where(RefreshToken.jti == claims["jti"]))
    ).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        raise AuthError("Refresh token is not valid")
    user = (await session.execute(select(User).where(User.id == claims["sub"]))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User is not active")
    stored.revoked_at = datetime.now(UTC)
    access = create_access_token(user_id=user.id, email=user.email, roles=list(user.roles or []), scopes=[])
    new_refresh = create_refresh_token(user_id=user.id)
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=decode_token(new_refresh, expected_type="refresh")["jti"],
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=Principal(user, auth_method="jwt").to_dict(),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, session: SessionDep, principal: PrincipalDep) -> None:
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    stored = (
        await session.execute(select(RefreshToken).where(RefreshToken.jti == claims["jti"]))
    ).scalar_one_or_none()
    if stored is not None:
        stored.revoked_at = datetime.now(UTC)
    await write_audit(
        session, principal=principal, action="auth.logout", resource_type="user", resource_id=principal.id
    )


@router.get("/me")
async def me(principal: PrincipalDep) -> dict[str, Any]:
    return principal.to_dict()


class MfaEnrolResponse(BaseModel):
    secret: str
    provisioning_uri: str
    enrolment_token: str
    expires_in: int


class MfaStepUpRequest(BaseModel):
    """Proof that the caller is the account holder, not merely the holder of its session.

    Changing the second factor is what an attacker who has stolen a session does next, so
    it is gated on something the session does not carry: the password, plus the factor
    already in force when there is one.
    """

    password: str
    mfa_code: str | None = None


async def _require_step_up(
    session: AsyncSession, principal: Principal, payload: MfaStepUpRequest, *, action: str
) -> None:
    user = principal.user
    if not user.hashed_password:
        raise ForbiddenError(
            "This account signs in through SSO; its second factor is managed by the identity provider"
        )
    if not verify_password(payload.password, user.hashed_password):
        await write_audit(
            session,
            principal=principal,
            action=action,
            resource_type="user",
            resource_id=principal.id,
            outcome="failure",
            severity="warning",
            details={"reason": "bad_password"},
        )
        raise AuthError("Current password is incorrect")
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise AuthError("Current MFA code required", details={"mfa_required": True})
        if not verify_totp(user.mfa_secret or "", payload.mfa_code):
            await write_audit(
                session,
                principal=principal,
                action=action,
                resource_type="user",
                resource_id=principal.id,
                outcome="failure",
                severity="warning",
                details={"reason": "bad_mfa_code"},
            )
            raise AuthError("Invalid MFA code")


async def _revoke_all_sessions(session: AsyncSession, user_id: str) -> int:
    """Revoke every live refresh token for a user.

    A second-factor change is only worth as much as the sessions it invalidates: leaving
    the tokens minted before it alive would let whoever prompted the change keep using them.
    """
    tokens = (
        (
            await session.execute(
                select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
    return len(tokens)


@router.post("/mfa/enrol", response_model=MfaEnrolResponse)
async def enrol_mfa(
    payload: MfaStepUpRequest, session: SessionDep, principal: PrincipalDep
) -> MfaEnrolResponse:
    await _require_step_up(session, principal, payload, action="auth.mfa.enrol")
    secret = new_totp_secret()
    return MfaEnrolResponse(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, principal.email),
        enrolment_token=create_mfa_enrolment_token(user_id=principal.id, secret=secret),
        expires_in=MFA_ENROL_TTL_SECONDS,
    )


class MfaVerifyRequest(BaseModel):
    code: str
    enrolment_token: str


@router.post("/mfa/verify")
async def verify_mfa(
    payload: MfaVerifyRequest, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    claims = decode_token(payload.enrolment_token, expected_type=MFA_ENROL)
    if claims.get("sub") != principal.id:
        raise ForbiddenError("This enrolment was issued to a different account")
    secret = str(claims.get("mfa_secret") or "")
    if not secret:
        raise ValidationError("Start MFA enrolment first")
    if not verify_totp(secret, payload.code):
        raise AuthError("Invalid MFA code")
    principal.user.mfa_secret = secret
    principal.user.mfa_enabled = True
    revoked = await _revoke_all_sessions(session, principal.id)
    await write_audit(
        session,
        principal=principal,
        action="auth.mfa.enabled",
        resource_type="user",
        resource_id=principal.id,
        severity="warning",
        details={"sessions_revoked": revoked},
    )
    return {"mfa_enabled": True, "sessions_revoked": revoked}


@router.delete("/mfa")
async def disable_mfa(
    payload: MfaStepUpRequest, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    await _require_step_up(session, principal, payload, action="auth.mfa.disabled")
    principal.user.mfa_enabled = False
    principal.user.mfa_secret = None
    revoked = await _revoke_all_sessions(session, principal.id)
    await write_audit(
        session,
        principal=principal,
        action="auth.mfa.disabled",
        resource_type="user",
        resource_id=principal.id,
        severity="warning",
        details={"sessions_revoked": revoked},
    )
    return {"mfa_enabled": False, "sessions_revoked": revoked}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12)


@router.post("/password")
async def change_password(
    payload: ChangePasswordRequest, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    if not verify_password(payload.current_password, principal.user.hashed_password or ""):
        raise AuthError("Current password is incorrect")
    principal.user.hashed_password = hash_password(payload.new_password)
    await write_audit(
        session,
        principal=principal,
        action="auth.password.changed",
        resource_type="user",
        resource_id=principal.id,
        severity="warning",
    )
    return {"updated": True}


# --- API keys ----------------------------------------------------------------
class ApiKeyCreate(BaseModel):
    name: str
    #: Permission names (``agent:execute``, ``tool:invoke``, ...) the key may use. The key's
    #: effective permissions are these intersected with the owner's roles, so a scope the
    #: owner does not hold grants nothing. Empty means the key inherits the owner's roles.
    scopes: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = 120
    expires_in_days: int | None = None


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    principal.require(Permission.SECURITY_ADMIN)
    if unknown := unknown_scopes(payload.scopes):
        raise ValidationError(
            "Unknown scope(s)",
            details={"unknown": unknown, "valid": sorted(str(p) for p in Permission)},
        )
    full_key, prefix, hashed = generate_api_key()
    api_key = ApiKey(
        name=payload.name,
        prefix=prefix,
        hashed_key=hashed,
        user_id=principal.id,
        scopes=payload.scopes,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        expires_at=datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None,
    )
    session.add(api_key)
    await session.flush()
    await write_audit(
        session,
        principal=principal,
        action="apikey.created",
        resource_type="api_key",
        resource_id=api_key.id,
        severity="warning",
        details={"name": payload.name},
    )
    return {
        "id": api_key.id,
        "name": api_key.name,
        "prefix": api_key.prefix,
        "api_key": full_key,
        "warning": "Store this key now - it cannot be retrieved again.",
        "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
    }


@router.get("/api-keys")
async def list_api_keys(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.SECURITY_READ)
    keys = (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    return [
        {
            "id": k.id,
            "name": k.name,
            "prefix": k.prefix,
            "scopes": k.scopes,
            "rate_limit_per_minute": k.rate_limit_per_minute,
            "usage_count": k.usage_count,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "revoked": k.revoked_at is not None,
            "created_at": k.created_at.isoformat(),
        }
        for k in keys
    ]


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.SECURITY_ADMIN)
    api_key = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if api_key is None:
        raise NotFoundError("API key not found")
    api_key.revoked_at = datetime.now(UTC)
    await write_audit(
        session,
        principal=principal,
        action="apikey.revoked",
        resource_type="api_key",
        resource_id=key_id,
        severity="warning",
    )
    return {"revoked": True, "id": key_id}


# --- users -------------------------------------------------------------------
class UserCreate(BaseModel):
    email: str
    full_name: str
    password: str = Field(min_length=12)
    roles: list[str] = Field(default_factory=lambda: ["viewer"])
    department: str = "Unassigned"

    _normalise_email = field_validator("email")(lambda cls, v: _validate_email(v))


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.USER_ADMIN)
    unknown = [r for r in payload.roles if r not in ROLE_PERMISSIONS]
    if unknown:
        raise ValidationError(
            "Unknown role(s)", details={"unknown": unknown, "valid": sorted(ROLE_PERMISSIONS)}
        )
    existing = (
        await session.execute(select(User).where(func.lower(User.email) == payload.email.lower()))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("A user with that email already exists")
    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        roles=payload.roles,
        department=payload.department,
    )
    session.add(user)
    await session.flush()
    await write_audit(
        session,
        principal=principal,
        action="user.created",
        resource_type="user",
        resource_id=user.id,
        severity="warning",
        details={"roles": payload.roles},
    )
    return {"id": user.id, "email": user.email, "roles": user.roles}


@router.get("/users")
async def list_users(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.USER_ADMIN)
    users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    return [
        {
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name,
            "roles": u.roles,
            "department": u.department,
            "is_active": u.is_active,
            "mfa_enabled": u.mfa_enabled,
            "is_service_account": u.is_service_account,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "locked": bool(u.locked_until and u.locked_until > datetime.now(UTC)),
        }
        for u in users
    ]


class UserUpdate(BaseModel):
    roles: list[str] | None = None
    is_active: bool | None = None
    department: str | None = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, payload: UserUpdate, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    principal.require(Permission.USER_ADMIN)
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    if payload.roles is not None:
        unknown = [r for r in payload.roles if r not in ROLE_PERMISSIONS]
        if unknown:
            raise ValidationError("Unknown role(s)", details={"unknown": unknown})
        user.roles = payload.roles
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.department is not None:
        user.department = payload.department
    await write_audit(
        session,
        principal=principal,
        action="user.updated",
        resource_type="user",
        resource_id=user_id,
        severity="warning",
        details=payload.model_dump(exclude_none=True),
    )
    return {"id": user.id, "roles": user.roles, "is_active": user.is_active, "department": user.department}


@router.get("/roles")
async def list_roles(principal: PrincipalDep) -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "description": ROLE_DESCRIPTIONS.get(role, ""),
            "permissions": sorted(str(p) for p in perms),
            "permission_count": len(perms),
        }
        for role, perms in ROLE_PERMISSIONS.items()
    ]


@router.get("/sso")
async def sso_configuration() -> dict[str, Any]:
    configured = bool(settings.keycloak_url and settings.keycloak_realm and settings.keycloak_client_id)
    info: dict[str, Any] = {"provider": "keycloak", "configured": configured}
    if configured:
        base = f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"
        info.update(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/protocol/openid-connect/auth",
                "token_endpoint": f"{base}/protocol/openid-connect/token",
                "jwks_uri": f"{base}/protocol/openid-connect/certs",
                "client_id": settings.keycloak_client_id,
                # The browser must not build the authorization URL itself: the PKCE
                # verifier and nonce this platform will check are minted and kept here.
                "start_login_endpoint": f"{settings.api_prefix}/auth/sso/authorize",
                "pkce": "S256",
                "requires_verified_email": True,
            }
        )
    else:
        info["required_settings"] = [
            "KEYCLOAK_URL",
            "KEYCLOAK_REALM",
            "KEYCLOAK_CLIENT_ID",
            "KEYCLOAK_CLIENT_SECRET",
        ]
    return info


SSO_TRANSACTION_TTL_SECONDS = 600


def _sso_base() -> str:
    if not (settings.keycloak_url and settings.keycloak_realm and settings.keycloak_client_id):
        raise ValidationError("SSO is not configured on this deployment")
    return f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"


class SsoAuthorizeRequest(BaseModel):
    redirect_uri: str
    #: Where to send the browser once this platform has issued its own tokens. Returned
    #: with the transaction so the front end does not have to keep it itself.
    return_to: str | None = None


class SsoAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    expires_in: int


@router.post("/sso/authorize", response_model=SsoAuthorizeResponse)
async def sso_authorize(payload: SsoAuthorizeRequest) -> SsoAuthorizeResponse:
    """Start an OIDC login and record what the callback must later prove.

    Without this step the callback accepted any code for any redirect URI, which is what
    makes authorization-code injection work: an attacker who obtains a code (their own, or
    one phished from a victim) can post it to the callback and be issued this platform's
    tokens. The `state` binds the callback to a browser that started here, and the PKCE
    verifier -- kept server-side and never sent to the browser -- binds the code exchange
    to this transaction.
    """
    base = _sso_base()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    await cache.set(
        f"sso:txn:{state}",
        {
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": payload.redirect_uri,
            "return_to": payload.return_to,
        },
        ttl=SSO_TRANSACTION_TTL_SECONDS,
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.keycloak_client_id or "",
            "redirect_uri": payload.redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return SsoAuthorizeResponse(
        authorization_url=f"{base}/protocol/openid-connect/auth?{query}",
        state=state,
        expires_in=SSO_TRANSACTION_TTL_SECONDS,
    )


async def _oidc_signing_keys(base: str) -> dict[str, Any]:
    """The provider's JWKS, cached briefly so every login does not refetch it."""
    from app.llm.base import http_client

    cached = await cache.get(f"sso:jwks:{base}", scope="sso")
    if cached:
        return cached
    resp = await http_client().get(f"{base}/protocol/openid-connect/certs", timeout=15.0)
    if resp.status_code >= 400:
        raise AuthError("Unable to fetch the SSO provider's signing keys")
    keys = resp.json()
    await cache.set(f"sso:jwks:{base}", keys, ttl=3600)
    return keys


async def _verified_id_token(base: str, id_token: str, *, nonce: str) -> dict[str, Any]:
    """Validate the ID token's signature, issuer, audience and nonce.

    The userinfo endpoint answers for whoever holds the access token; only the ID token
    says which client and which login transaction the identity was issued for. Skipping
    these checks is what lets a token minted for another client be replayed here.
    """
    try:
        claims = jwt.decode(
            id_token,
            await _oidc_signing_keys(base),
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=settings.keycloak_client_id,
            issuer=base,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        # A rejected identity assertion is a failed sign-in, not a server fault.
        raise AuthError("SSO identity token failed validation", details={"reason": str(exc)}) from exc
    if claims.get("nonce") != nonce:
        raise AuthError("SSO nonce does not match the login that was started")
    return claims


class SsoExchangeRequest(BaseModel):
    code: str
    state: str
    redirect_uri: str


@router.post("/sso/callback", response_model=TokenResponse)
async def sso_callback(payload: SsoExchangeRequest, session: SessionDep, request: Request) -> TokenResponse:
    base = _sso_base()
    from app.llm.base import http_client

    transaction = await cache.get(f"sso:txn:{payload.state}", scope="sso")
    if not transaction:
        raise AuthError("Unknown or expired SSO login. Start again.")
    # One code, one exchange: deleting before the exchange means a replayed callback finds
    # nothing, whether or not the exchange below succeeds.
    await cache.delete(f"sso:txn:{payload.state}")
    if payload.redirect_uri != transaction.get("redirect_uri"):
        raise AuthError("SSO redirect URI does not match the login that was started")

    resp = await http_client().post(
        f"{base}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": payload.code,
            "redirect_uri": payload.redirect_uri,
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret or "",
            "code_verifier": transaction["verifier"],
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise AuthError("SSO code exchange failed", details={"body": resp.text[:400]})
    tokens = resp.json()
    if not tokens.get("id_token"):
        raise AuthError("SSO provider returned no ID token; the 'openid' scope is required")
    claims = await _verified_id_token(base, tokens["id_token"], nonce=transaction["nonce"])
    userinfo = await http_client().get(
        f"{base}/protocol/openid-connect/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=30.0,
    )
    if userinfo.status_code >= 400:
        raise AuthError("Unable to read SSO user profile")
    profile = userinfo.json()
    # Claims from the signed ID token win over the userinfo response: the former is bound to
    # this client and this transaction, the latter only to whoever holds the access token.
    subject = str(claims.get("sub") or profile.get("sub") or "")
    email = str(claims.get("email") or profile.get("email") or "").lower()
    email_verified = bool(claims.get("email_verified", profile.get("email_verified", False)))
    if not email:
        raise AuthError("SSO profile has no email claim")
    if not subject:
        raise AuthError("SSO profile has no subject claim")
    if not email_verified:
        # Email is the only thing tying this identity to a platform account, so an
        # unverified one is an invitation to register the address of somebody who already
        # has access and inherit their roles.
        await write_audit(
            session,
            principal=None,
            action="auth.sso.login",
            resource_type="user",
            resource_id=email,
            outcome="failure",
            severity="warning",
            details={"reason": "email_not_verified"},
            request=request,
        )
        raise AuthError(
            "The identity provider has not verified this email address, so it cannot be used to sign in here"
        )

    user = (await session.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if user is not None and user.sso_subject and user.sso_subject != subject:
        # The address matches an account already bound to a different provider subject.
        # Re-pointing it on the strength of a matching email is account takeover.
        await write_audit(
            session,
            principal=None,
            action="auth.sso.login",
            resource_type="user",
            resource_id=user.id,
            outcome="failure",
            severity="critical",
            details={"reason": "subject_mismatch"},
            request=request,
        )
        raise AuthError("This account is linked to a different identity provider subject")
    if user is None:
        user = User(
            email=email,
            full_name=claims.get("name") or profile.get("name") or email,
            roles=["viewer"],
            sso_subject=subject,
            sso_provider="keycloak",
        )
        session.add(user)
        await session.flush()
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    user.last_login_at = datetime.now(UTC)
    user.sso_subject = subject
    user.sso_provider = "keycloak"
    await write_audit(
        session,
        principal=None,
        action="auth.sso.login",
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    access = create_access_token(user_id=user.id, email=user.email, roles=list(user.roles or []), scopes=[])
    refresh = create_refresh_token(user_id=user.id)
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=decode_token(refresh, expected_type="refresh")["jti"],
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=Principal(user, auth_method="sso").to_dict(),
    )
