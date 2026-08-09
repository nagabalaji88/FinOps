"""FastAPI dependencies: authentication, RBAC, rate limiting and audit context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthError, ForbiddenError
from app.core.logging import user_id_ctx
from app.core.rbac import Permission, has_permission, permissions_for_roles
from app.core.resilience import TokenBucketLimiter
from app.core.security import decode_token, hash_api_key
from app.db.models.identity import ApiKey, AuditLog, User
from app.db.session import get_session

bearer_scheme = HTTPBearer(auto_error=False)
limiter = TokenBucketLimiter(settings.rate_limit_per_minute)


async def db_session() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session)]


class Principal:
    """Authenticated caller: a user or an API-key-backed service identity."""

    def __init__(self, user: User, *, auth_method: str, api_key: ApiKey | None = None):
        self.user = user
        self.id = user.id
        self.email = user.email
        self.full_name = user.full_name
        self.roles: list[str] = list(user.roles or [])
        self.department = user.department
        self.auth_method = auth_method
        self.api_key = api_key
        self.permissions = permissions_for_roles(self.roles)

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise ForbiddenError(
                f"Missing permission '{permission}'",
                details={"required": str(permission), "roles": self.roles},
            )

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name,
            "roles": self.roles,
            "department": self.department,
            "permissions": sorted(str(p) for p in self.permissions),
            "auth_method": self.auth_method,
            "mfa_enabled": self.user.mfa_enabled,
        }


async def current_principal(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    if x_api_key:
        principal = await _principal_from_api_key(session, x_api_key)
    elif credentials and credentials.scheme.lower() == "bearer":
        principal = await _principal_from_jwt(session, credentials.credentials)
    else:
        raise AuthError("Authentication required", details={"schemes": ["Bearer JWT", "X-API-Key"]})

    user_id_ctx.set(principal.id)
    request.state.principal = principal
    key = principal.api_key.id if principal.api_key else principal.id
    rate = principal.api_key.rate_limit_per_minute if principal.api_key else settings.rate_limit_per_minute
    if rate != limiter.capacity:
        await TokenBucketLimiter(rate).enforce(key)
    else:
        await limiter.enforce(key)
    return principal


async def _principal_from_jwt(session: AsyncSession, token: str) -> Principal:
    payload = decode_token(token)
    user = (await session.execute(select(User).where(User.id == payload.get("sub")))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User is not active")
    return Principal(user, auth_method="jwt")


async def _principal_from_api_key(session: AsyncSession, raw_key: str) -> Principal:
    hashed = hash_api_key(raw_key)
    api_key = (await session.execute(select(ApiKey).where(ApiKey.hashed_key == hashed))).scalar_one_or_none()
    if api_key is None:
        raise AuthError("Invalid API key")
    if api_key.revoked_at is not None:
        raise AuthError("API key has been revoked")
    if api_key.expires_at and api_key.expires_at < datetime.now(UTC):
        raise AuthError("API key has expired")
    user = (await session.execute(select(User).where(User.id == api_key.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("API key owner is not active")
    api_key.last_used_at = datetime.now(UTC)
    api_key.usage_count += 1
    return Principal(user, auth_method="api_key", api_key=api_key)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_permission(permission: Permission):
    async def _guard(principal: PrincipalDep) -> Principal:
        principal.require(permission)
        return principal

    return _guard


async def optional_principal(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal | None:
    try:
        return await current_principal(request, session, credentials, x_api_key)
    except AuthError:
        return None


async def write_audit(
    session: AsyncSession,
    *,
    principal: Principal | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "success",
    severity: str = "info",
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    from app.core.logging import request_id_ctx, trace_id_ctx

    session.add(
        AuditLog(
            actor_id=principal.id if principal else None,
            actor_email=principal.email if principal else None,
            actor_type=(principal.auth_method if principal else "system"),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            severity=severity,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            request_id=request_id_ctx.get(),
            trace_id=trace_id_ctx.get(),
            details=details or {},
        )
    )


def check_permission(principal: Principal, permission: Permission) -> bool:
    return has_permission(principal.roles, permission)
