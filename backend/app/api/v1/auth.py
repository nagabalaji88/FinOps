"""Authentication: password login, MFA, refresh, API keys, SSO metadata."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.config import settings
from app.core.errors import AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.rbac import ROLE_DESCRIPTIONS, ROLE_PERMISSIONS, Permission
from app.core.security import (
    create_access_token,
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
    user = (
        await session.execute(select(User).where(func.lower(User.email) == payload.email.lower()))
    ).scalar_one_or_none()
    if user is None or not user.hashed_password:
        await write_audit(
            session,
            principal=None,
            action="auth.login",
            resource_type="user",
            resource_id=payload.email,
            outcome="failure",
            severity="warning",
            details={"reason": "unknown_user"},
            request=request,
        )
        raise AuthError("Invalid credentials")
    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise ForbiddenError(
            "Account temporarily locked", details={"locked_until": user.locked_until.isoformat()}
        )
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    if not verify_password(payload.password, user.hashed_password):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_MINUTES)
        await write_audit(
            session,
            principal=None,
            action="auth.login",
            resource_type="user",
            resource_id=user.id,
            outcome="failure",
            severity="warning",
            details={"reason": "bad_password", "attempts": user.failed_login_count},
            request=request,
        )
        raise AuthError("Invalid credentials")
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise AuthError("MFA code required", details={"mfa_required": True})
        if not verify_totp(user.mfa_secret or "", payload.mfa_code):
            await write_audit(
                session,
                principal=None,
                action="auth.mfa",
                resource_type="user",
                resource_id=user.id,
                outcome="failure",
                severity="warning",
                request=request,
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
    from app.api.deps import Principal

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
    from app.api.deps import Principal

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


@router.post("/mfa/enrol", response_model=MfaEnrolResponse)
async def enrol_mfa(session: SessionDep, principal: PrincipalDep) -> MfaEnrolResponse:
    secret = new_totp_secret()
    principal.user.mfa_secret = secret
    await session.flush()
    return MfaEnrolResponse(secret=secret, provisioning_uri=totp_provisioning_uri(secret, principal.email))


class MfaVerifyRequest(BaseModel):
    code: str


@router.post("/mfa/verify")
async def verify_mfa(
    payload: MfaVerifyRequest, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    if not principal.user.mfa_secret:
        raise ValidationError("Start MFA enrolment first")
    if not verify_totp(principal.user.mfa_secret, payload.code):
        raise AuthError("Invalid MFA code")
    principal.user.mfa_enabled = True
    await write_audit(
        session,
        principal=principal,
        action="auth.mfa.enabled",
        resource_type="user",
        resource_id=principal.id,
    )
    return {"mfa_enabled": True}


@router.delete("/mfa")
async def disable_mfa(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.user.mfa_enabled = False
    principal.user.mfa_secret = None
    await write_audit(
        session,
        principal=principal,
        action="auth.mfa.disabled",
        resource_type="user",
        resource_id=principal.id,
        severity="warning",
    )
    return {"mfa_enabled": False}


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
    scopes: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = 120
    expires_in_days: int | None = None


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    principal.require(Permission.SECURITY_ADMIN)
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


class SsoExchangeRequest(BaseModel):
    code: str
    redirect_uri: str


@router.post("/sso/callback", response_model=TokenResponse)
async def sso_callback(payload: SsoExchangeRequest, session: SessionDep, request: Request) -> TokenResponse:
    if not (settings.keycloak_url and settings.keycloak_realm and settings.keycloak_client_id):
        raise ValidationError("SSO is not configured on this deployment")
    from app.llm.base import http_client

    base = f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"
    resp = await http_client().post(
        f"{base}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": payload.code,
            "redirect_uri": payload.redirect_uri,
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret or "",
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise AuthError("SSO code exchange failed", details={"body": resp.text[:400]})
    tokens = resp.json()
    userinfo = await http_client().get(
        f"{base}/protocol/openid-connect/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=30.0,
    )
    if userinfo.status_code >= 400:
        raise AuthError("Unable to read SSO user profile")
    profile = userinfo.json()
    email = (profile.get("email") or "").lower()
    if not email:
        raise AuthError("SSO profile has no email claim")
    user = (await session.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            full_name=profile.get("name") or email,
            roles=["viewer"],
            sso_subject=profile.get("sub"),
            sso_provider="keycloak",
        )
        session.add(user)
        await session.flush()
    user.last_login_at = datetime.now(UTC)
    user.sso_subject = profile.get("sub")
    user.sso_provider = "keycloak"
    await write_audit(
        session,
        principal=None,
        action="auth.sso.login",
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    from app.api.deps import Principal

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
