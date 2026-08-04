"""Approvals, log search/streaming, audit trail and the security dashboard."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.bus import APPROVAL_CHANNEL, bus
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.metrics import approvals_total
from app.core.rbac import ROLE_PERMISSIONS, Permission
from app.core.secrets import secret_manager
from app.core.security import mask_secret
from app.db.models.agents import Approval, Execution, LogRecord
from app.db.models.identity import ApiKey, AuditLog, FeatureFlag, StoredSecret, User
from app.engine.executor import engine

approvals_router = APIRouter(prefix="/approvals", tags=["approvals"])
logs_router = APIRouter(prefix="/logs", tags=["logs"])
security_router = APIRouter(prefix="/security", tags=["security"])


def _serialise_approval(a: Approval) -> dict[str, Any]:
    return {
        "id": a.id,
        "execution_id": a.execution_id,
        "agent_key": a.agent_key,
        "node": a.node,
        "title": a.title,
        "summary": a.summary,
        "payload": a.payload,
        "risk_level": a.risk_level,
        "required_role": a.required_role,
        "status": a.status,
        "requested_by": a.requested_by,
        "reviewer_email": a.reviewer_email,
        "comments": a.comments,
        "timeline": a.timeline or [],
        "created_at": a.created_at.isoformat(),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "expired": bool(a.expires_at and a.expires_at < datetime.now(UTC)
                        and a.status == "pending"),
    }


@approvals_router.get("")
async def list_approvals(session: SessionDep, principal: PrincipalDep,
                         status_filter: str = Query(default="pending", alias="status"),
                         agent_key: str | None = None,
                         limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    principal.require(Permission.APPROVAL_READ)
    stmt = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
    if status_filter and status_filter != "all":
        stmt = stmt.where(Approval.status.in_([s.strip() for s in status_filter.split(",")]))
    if agent_key:
        stmt = stmt.where(Approval.agent_key == agent_key)
    rows = (await session.execute(stmt)).scalars().all()
    counts = dict(
        (await session.execute(
            select(Approval.status, func.count(Approval.id)).group_by(Approval.status)
        )).all()
    )
    return {"counts": counts, "items": [_serialise_approval(a) for a in rows]}


@approvals_router.get("/{approval_id}")
async def get_approval(approval_id: str, session: SessionDep,
                       principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.APPROVAL_READ)
    approval = (
        await session.execute(select(Approval).where(Approval.id == approval_id))
    ).scalar_one_or_none()
    if approval is None:
        raise NotFoundError("Approval not found")
    execution = (
        await session.execute(select(Execution).where(Execution.id == approval.execution_id))
    ).scalar_one_or_none()
    payload = _serialise_approval(approval)
    payload["execution"] = {
        "id": execution.id, "status": execution.status, "agent_key": execution.agent_key,
        "input": execution.input, "trace_id": execution.trace_id,
        "cost_usd": execution.cost_usd, "user_email": execution.user_email,
    } if execution else None
    return payload


class DecisionRequest(BaseModel):
    decision: str = Field(description="approve|reject")
    comments: str | None = None


@approvals_router.post("/{approval_id}/decision")
async def decide_approval(approval_id: str, payload: DecisionRequest, request: Request,
                          session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.APPROVAL_DECIDE)
    approval = (
        await session.execute(select(Approval).where(Approval.id == approval_id))
    ).scalar_one_or_none()
    if approval is None:
        raise NotFoundError("Approval not found")
    if approval.status != "pending":
        raise ValidationError(f"Approval already {approval.status}")
    if approval.expires_at and approval.expires_at < datetime.now(UTC):
        approval.status = "expired"
        raise ValidationError("Approval request has expired")
    if approval.required_role not in principal.roles and "admin" not in principal.roles:
        raise ForbiddenError(f"Decision requires role '{approval.required_role}'")
    if approval.requested_by and approval.requested_by == principal.email \
            and "admin" not in principal.roles:
        raise ForbiddenError("Segregation of duties: you cannot approve your own request")
    if payload.decision not in {"approve", "reject"}:
        raise ValidationError("decision must be 'approve' or 'reject'")

    approved = payload.decision == "approve"
    now = datetime.now(UTC)
    approval.status = "approved" if approved else "rejected"
    approval.reviewer_id = principal.id
    approval.reviewer_email = principal.email
    approval.comments = payload.comments
    approval.decided_at = now
    approval.timeline = [*(approval.timeline or []),
                         {"at": now.isoformat(), "event": approval.status,
                          "by": principal.email, "comments": payload.comments}]
    approvals_total.labels(approval.agent_key, approval.status).inc()
    await write_audit(session, principal=principal, action=f"approval.{approval.status}",
                      resource_type="approval", resource_id=approval_id, severity="warning",
                      details={"execution_id": approval.execution_id,
                               "risk": approval.risk_level}, request=request)
    await session.commit()

    await bus.publish(APPROVAL_CHANNEL, {"type": "approval.decided", "approval_id": approval.id,
                                         "status": approval.status,
                                         "execution_id": approval.execution_id,
                                         "reviewer": principal.email})
    execution = await engine.resume_after_approval(
        session, approval.execution_id, approved=approved, approval_payload=approval.payload or {}
    )
    return {"approval_id": approval.id, "status": approval.status,
            "execution_id": approval.execution_id, "execution_status": execution.status}


@approvals_router.get("/stream/live")
async def stream_approvals(request: Request, principal: PrincipalDep):
    principal.require(Permission.APPROVAL_READ)

    async def generator():
        async with bus.subscribe(APPROVAL_CHANNEL) as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    _, event = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    continue
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}

    return EventSourceResponse(generator(), ping=20)


# --- logs --------------------------------------------------------------------
@logs_router.get("")
async def search_logs(
    session: SessionDep,
    principal: PrincipalDep,
    q: str | None = None,
    level: str | None = None,
    agent_key: str | None = None,
    execution_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    principal.require(Permission.LOG_READ)
    since = datetime.now(UTC) - timedelta(minutes=since_minutes)
    stmt = select(LogRecord).where(LogRecord.timestamp >= since)
    count_stmt = select(func.count(LogRecord.id)).where(LogRecord.timestamp >= since)
    filters = []
    if level:
        filters.append(LogRecord.level.in_([lv.strip().upper() for lv in level.split(",")]))
    if agent_key:
        filters.append(LogRecord.agent_key == agent_key)
    if execution_id:
        filters.append(LogRecord.execution_id == execution_id)
    if correlation_id:
        filters.append(LogRecord.correlation_id == correlation_id)
    if trace_id:
        filters.append(LogRecord.trace_id == trace_id)
    if q:
        filters.append(or_(LogRecord.message.ilike(f"%{q}%"), LogRecord.logger.ilike(f"%{q}%")))
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(LogRecord.timestamp.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [
            {"id": r.id, "timestamp": r.timestamp.isoformat(), "level": r.level,
             "logger": r.logger, "message": r.message, "agent_key": r.agent_key,
             "execution_id": r.execution_id, "correlation_id": r.correlation_id,
             "trace_id": r.trace_id, "span_id": r.span_id, "user_email": r.user_email,
             "attributes": r.attributes}
            for r in rows
        ],
    }


@logs_router.get("/export")
async def export_logs(session: SessionDep, principal: PrincipalDep,
                      execution_id: str | None = None,
                      since_minutes: int = Query(default=1440, ge=1, le=43200),
                      fmt: str = Query(default="jsonl", pattern="^(jsonl|csv)$")):
    principal.require(Permission.LOG_READ)
    from fastapi.responses import StreamingResponse

    since = datetime.now(UTC) - timedelta(minutes=since_minutes)
    stmt = select(LogRecord).where(LogRecord.timestamp >= since).order_by(LogRecord.timestamp)
    if execution_id:
        stmt = stmt.where(LogRecord.execution_id == execution_id)
    rows = (await session.execute(stmt.limit(50000))).scalars().all()

    def render():
        if fmt == "csv":
            yield "timestamp,level,logger,agent_key,execution_id,correlation_id,message\n"
            for r in rows:
                message = (r.message or "").replace('"', '""')
                yield (f'{r.timestamp.isoformat()},{r.level},{r.logger},{r.agent_key or ""},'
                       f'{r.execution_id or ""},{r.correlation_id or ""},"{message}"\n')
        else:
            for r in rows:
                yield json.dumps({
                    "timestamp": r.timestamp.isoformat(), "level": r.level, "logger": r.logger,
                    "message": r.message, "agent_key": r.agent_key,
                    "execution_id": r.execution_id, "correlation_id": r.correlation_id,
                    "trace_id": r.trace_id, "attributes": r.attributes,
                }) + "\n"

    media = "text/csv" if fmt == "csv" else "application/x-ndjson"
    filename = f"finops-logs-{datetime.now(UTC):%Y%m%dT%H%M%S}.{fmt}"
    return StreamingResponse(render(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@logs_router.get("/stream")
async def stream_logs(request: Request, principal: PrincipalDep,
                      level: str | None = None, agent_key: str | None = None):
    """Live log tail: subscribes to all execution channels and filters."""
    principal.require(Permission.LOG_READ)
    levels = {lv.strip().upper() for lv in level.split(",")} if level else None

    async def generator():
        async with bus.subscribe("*") as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    _, event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    continue
                if agent_key and event.get("agent_key") != agent_key:
                    continue
                payload = event.get("payload") or {}
                event_level = "ERROR" if "failed" in str(event.get("type", "")) else "INFO"
                if levels and event_level not in levels:
                    continue
                yield {"event": "log", "data": json.dumps({
                    "timestamp": event.get("timestamp"),
                    "level": event_level,
                    "logger": f"agent.{event.get('agent_key', 'platform')}",
                    "message": f"{event.get('type')} {payload.get('node') or ''}".strip(),
                    "execution_id": event.get("execution_id"),
                    "correlation_id": event.get("correlation_id"),
                    "trace_id": event.get("trace_id"),
                    "attributes": payload,
                })}

    return EventSourceResponse(generator(), ping=15)


# --- security ----------------------------------------------------------------
@security_router.get("/overview")
async def security_overview(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.SECURITY_READ)
    now = datetime.now(UTC)
    users = (await session.execute(select(User))).scalars().all()
    keys = (await session.execute(select(ApiKey))).scalars().all()
    secrets_rows = (await session.execute(select(StoredSecret))).scalars().all()
    flags = (await session.execute(select(FeatureFlag))).scalars().all()
    failed_logins = int(
        (await session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "auth.login", AuditLog.outcome == "failure",
                AuditLog.created_at >= now - timedelta(days=1),
            )
        )).scalar_one()
    )
    role_distribution: dict[str, int] = {}
    for user in users:
        for role in user.roles or []:
            role_distribution[role] = role_distribution.get(role, 0) + 1

    return {
        "authentication": {
            "users_total": len(users),
            "active_users": sum(1 for u in users if u.is_active),
            "service_accounts": sum(1 for u in users if u.is_service_account),
            "mfa_enabled": sum(1 for u in users if u.mfa_enabled),
            "mfa_coverage_pct": round(
                sum(1 for u in users if u.mfa_enabled) / len(users) * 100, 2) if users else 0.0,
            "locked_accounts": sum(1 for u in users
                                   if u.locked_until and u.locked_until > now),
            "failed_logins_24h": failed_logins,
            "sso_configured": bool(settings.keycloak_url and settings.keycloak_realm),
            "sso_users": sum(1 for u in users if u.sso_provider),
        },
        "authorisation": {
            "roles": len(ROLE_PERMISSIONS),
            "permissions": len({p for perms in ROLE_PERMISSIONS.values() for p in perms}),
            "role_distribution": role_distribution,
        },
        "api_keys": {
            "total": len(keys),
            "active": sum(1 for k in keys if not k.revoked_at
                          and (not k.expires_at or k.expires_at > now)),
            "revoked": sum(1 for k in keys if k.revoked_at),
            "expiring_30d": sum(1 for k in keys if k.expires_at
                                and now < k.expires_at < now + timedelta(days=30)),
        },
        "secrets": {
            "backend": secret_manager.backend,
            "vault_connected": secret_manager.healthy,
            "stored": len(secrets_rows),
            "rotation_due": sum(
                1 for s in secrets_rows
                if s.rotated_at and (now - s.rotated_at).days > s.rotation_interval_days
            ),
        },
        "feature_flags": [
            {"key": f.key, "enabled": f.enabled, "rollout_percentage": f.rollout_percentage,
             "description": f.description, "updated_by": f.updated_by}
            for f in flags
        ],
        "posture": {
            "jwt_algorithm": settings.jwt_algorithm,
            "access_token_ttl_seconds": settings.access_token_ttl_seconds,
            "default_secret_in_use": settings.jwt_secret.startswith("change-me"),
            "tls_termination": "nginx/ingress",
            "data_retention_days": settings.data_retention_days,
            "zero_trust": {
                "authn_required_on_all_endpoints": True,
                "rbac_enforced": True,
                "audit_logging": True,
                "segregation_of_duties": True,
            },
        },
    }


@security_router.get("/audit")
async def audit_trail(session: SessionDep, principal: PrincipalDep,
                      action: str | None = None, actor: str | None = None,
                      resource_type: str | None = None,
                      since_hours: int = Query(default=168, ge=1, le=8760),
                      limit: int = Query(default=200, ge=1, le=2000),
                      offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    principal.require(Permission.AUDIT_READ)
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    stmt = select(AuditLog).where(AuditLog.created_at >= since)
    count_stmt = select(func.count(AuditLog.id)).where(AuditLog.created_at >= since)
    for column, value in [(AuditLog.action, action), (AuditLog.actor_email, actor),
                          (AuditLog.resource_type, resource_type)]:
        if value:
            stmt = stmt.where(column == value)
            count_stmt = count_stmt.where(column == value)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {"id": r.id, "timestamp": r.created_at.isoformat(), "actor_email": r.actor_email,
             "actor_type": r.actor_type, "action": r.action, "resource_type": r.resource_type,
             "resource_id": r.resource_id, "outcome": r.outcome, "severity": r.severity,
             "ip_address": r.ip_address, "request_id": r.request_id, "trace_id": r.trace_id,
             "details": r.details}
            for r in rows
        ],
    }


class SecretUpsert(BaseModel):
    name: str
    value: str
    category: str = "integration"
    rotation_interval_days: int = 90


@security_router.get("/secrets")
async def list_secrets(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.SECURITY_READ)
    rows = (await session.execute(select(StoredSecret).order_by(StoredSecret.name))).scalars().all()
    return [
        {"id": s.id, "name": s.name, "category": s.category, "hint": s.hint,
         "backend": s.backend, "rotation_interval_days": s.rotation_interval_days,
         "rotated_at": s.rotated_at.isoformat() if s.rotated_at else None,
         "created_by": s.created_by, "created_at": s.created_at.isoformat()}
        for s in rows
    ]


@security_router.put("/secrets")
async def upsert_secret(payload: SecretUpsert, request: Request, session: SessionDep,
                        principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.SECURITY_ADMIN)
    backend = await secret_manager.put(payload.name, payload.value)
    existing = (
        await session.execute(select(StoredSecret).where(StoredSecret.name == payload.name))
    ).scalar_one_or_none()
    sealed = secret_manager.seal(payload.value)
    if existing:
        existing.sealed_value = sealed
        existing.category = payload.category
        existing.hint = mask_secret(payload.value)
        existing.backend = backend
        existing.rotated_at = datetime.now(UTC)
        existing.rotation_interval_days = payload.rotation_interval_days
        record = existing
    else:
        record = StoredSecret(
            name=payload.name, category=payload.category, sealed_value=sealed,
            hint=mask_secret(payload.value), backend=backend,
            rotation_interval_days=payload.rotation_interval_days,
            rotated_at=datetime.now(UTC), created_by=principal.email,
        )
        session.add(record)
    secret_manager.invalidate(payload.name)
    await write_audit(session, principal=principal, action="secret.upserted",
                      resource_type="secret", resource_id=payload.name, severity="warning",
                      request=request)
    return {"name": payload.name, "backend": backend, "hint": mask_secret(payload.value)}


class FlagUpdate(BaseModel):
    enabled: bool
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    description: str | None = None


@security_router.put("/feature-flags/{key}")
async def upsert_flag(key: str, payload: FlagUpdate, request: Request, session: SessionDep,
                      principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.FEATURE_FLAG_ADMIN)
    flag = (
        await session.execute(select(FeatureFlag).where(FeatureFlag.key == key))
    ).scalar_one_or_none()
    if flag is None:
        flag = FeatureFlag(key=key, description=payload.description or "")
        session.add(flag)
    flag.enabled = payload.enabled
    flag.rollout_percentage = payload.rollout_percentage
    if payload.description is not None:
        flag.description = payload.description
    flag.updated_by = principal.email
    await write_audit(session, principal=principal, action="flag.updated",
                      resource_type="feature_flag", resource_id=key,
                      details=payload.model_dump(), request=request)
    return {"key": key, "enabled": flag.enabled, "rollout_percentage": flag.rollout_percentage}
