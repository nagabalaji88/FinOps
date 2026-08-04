"""Agent catalogue, lifecycle, configuration, versioning and execution submission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.agents.registry import ROADMAP, agent_registry
from app.core.bus import AGENT_CHANNEL, bus
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.rbac import Permission
from app.db.models.agents import Agent, AgentVersion, Approval, Execution
from app.engine.executor import engine
from app.engine.nodes import GRAPH_DEFINITION, GRAPH_EDGES
from app.tools.base import registry as tool_registry

router = APIRouter(prefix="/agents", tags=["agents"])


async def _agent_metrics(session: SessionDep, agent_key: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    day_start = now - timedelta(hours=24)
    row = (
        await session.execute(
            select(
                func.count(Execution.id),
                func.avg(Execution.latency_ms),
                func.sum(Execution.cost_usd),
                func.sum(Execution.retry_count),
            ).where(Execution.agent_key == agent_key, Execution.created_at >= day_start)
        )
    ).one()
    total_24h, avg_latency, cost_24h, retries = row
    statuses = (
        await session.execute(
            select(Execution.status, func.count(Execution.id))
            .where(Execution.agent_key == agent_key, Execution.created_at >= day_start)
            .group_by(Execution.status)
        )
    ).all()
    status_map = {s: c for s, c in statuses}
    total_all = int(
        (await session.execute(
            select(func.count(Execution.id)).where(Execution.agent_key == agent_key)
        )).scalar_one()
    )
    succeeded = status_map.get("succeeded", 0)
    failed = status_map.get("failed", 0) + status_map.get("timeout", 0)
    finished = succeeded + failed
    last = (
        await session.execute(
            select(Execution).where(Execution.agent_key == agent_key)
            .order_by(Execution.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    running = (
        await session.execute(
            select(Execution).where(
                Execution.agent_key == agent_key,
                Execution.status.in_(["running", "queued", "awaiting_approval"]),
            ).order_by(Execution.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    open_incidents = int(
        (await session.execute(
            select(func.count(Execution.id)).where(
                Execution.agent_key == agent_key, Execution.status.in_(["failed", "timeout"]),
                Execution.created_at >= day_start,
            )
        )).scalar_one()
    )
    pending_approvals = int(
        (await session.execute(
            select(func.count(Approval.id)).where(
                Approval.agent_key == agent_key, Approval.status == "pending"
            )
        )).scalar_one()
    )
    error_rate = round(failed / finished * 100, 2) if finished else 0.0
    health = "healthy"
    if error_rate > 25 or open_incidents >= 3:
        health = "unhealthy"
    elif error_rate > 5 or pending_approvals > 0:
        health = "degraded"

    return {
        "executions_total": total_all,
        "executions_24h": int(total_24h or 0),
        "succeeded_24h": succeeded,
        "failed_24h": failed,
        "running": status_map.get("running", 0) + status_map.get("queued", 0),
        "awaiting_approval": status_map.get("awaiting_approval", 0),
        "avg_latency_ms": int(avg_latency or 0),
        "cost_today_usd": round(float(cost_24h or 0), 4),
        "retry_count_24h": int(retries or 0),
        "error_rate_pct": error_rate,
        "success_rate_pct": round(succeeded / finished * 100, 2) if finished else 100.0,
        "open_incidents": open_incidents,
        "pending_approvals": pending_approvals,
        "health": health,
        "current_execution": {
            "id": running.id, "status": running.status,
            "started_at": running.started_at.isoformat() if running.started_at else None,
        } if running else None,
        "last_execution": {
            "id": last.id, "status": last.status, "latency_ms": last.latency_ms,
            "cost_usd": last.cost_usd,
            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
        } if last else None,
    }


def _serialise_agent(agent: Agent, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    config = agent.config or {}
    return {
        "id": agent.id,
        "key": agent.key,
        "name": agent.name,
        "description": agent.description,
        "category": agent.category,
        "availability": agent.availability,
        "lifecycle_state": agent.lifecycle_state,
        "owner": agent.owner,
        "owner_email": agent.owner_email,
        "department": agent.department,
        "version": agent.version,
        "is_builtin": agent.is_builtin,
        "tags": agent.tags or [],
        "tools": agent.tools or [],
        "knowledge_sources": agent.knowledge_sources or [],
        "model": config.get("model"),
        "temperature": config.get("temperature"),
        "memory_enabled": config.get("memory_enabled"),
        "requires_approval": config.get("final_approval_required"),
        "sla_latency_ms": agent.sla_latency_ms,
        "monthly_budget_usd": agent.monthly_budget_usd,
        "input_schema": config.get("input_schema", {}),
        "example_input": config.get("example_input", {}),
        "planned_quarter": config.get("planned_quarter"),
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
        "metrics": metrics or {},
    }


@router.get("")
async def list_agents(
    session: SessionDep,
    principal: PrincipalDep,
    availability: str | None = Query(default=None),
    include_metrics: bool = Query(default=True),
) -> list[dict[str, Any]]:
    principal.require(Permission.AGENT_READ)
    stmt = select(Agent).order_by(Agent.availability, Agent.name)
    if availability:
        stmt = stmt.where(Agent.availability == availability)
    agents = (await session.execute(stmt)).scalars().all()
    out = []
    for agent in agents:
        metrics = (
            await _agent_metrics(session, agent.key)
            if include_metrics and agent.availability == "implemented" else {}
        )
        out.append(_serialise_agent(agent, metrics))
    return out


@router.get("/graph")
async def execution_graph(principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.AGENT_READ)
    return {"nodes": GRAPH_DEFINITION, "edges": GRAPH_EDGES}


@router.get("/roadmap")
async def roadmap(principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.AGENT_READ)
    return ROADMAP


@router.get("/{agent_key}")
async def get_agent(agent_key: str, session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.AGENT_READ)
    agent = (
        await session.execute(select(Agent).where(Agent.key == agent_key))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"Agent '{agent_key}' not found")
    metrics = await _agent_metrics(session, agent_key) if agent.availability == "implemented" else {}
    payload = _serialise_agent(agent, metrics)
    payload["config"] = agent.config or {}
    payload["tool_details"] = [
        {
            "name": t.name, "description": t.description, "category": t.category,
            "requires_approval": t.requires_approval, "writes_data": t.writes_data,
            "timeout_seconds": t.timeout_seconds, "schema": t.json_schema,
        }
        for t in tool_registry.by_names(agent.tools or [])
    ]
    return payload


class ExecuteRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = None
    wait: bool = False


@router.post("/{agent_key}/execute", status_code=status.HTTP_202_ACCEPTED)
async def execute_agent(
    agent_key: str,
    payload: ExecuteRequest,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    principal.require(Permission.AGENT_EXECUTE)
    execution = await engine.submit(
        session,
        agent_key=agent_key,
        payload=payload.input,
        user=principal,
        trigger="manual",
        thread_id=payload.thread_id,
        request_id=request.headers.get("x-request-id"),
        wait=payload.wait,
    )
    await write_audit(session, principal=principal, action="agent.execute", resource_type="agent",
                      resource_id=agent_key, details={"execution_id": execution.id},
                      request=request)
    return {
        "execution_id": execution.id,
        "agent_key": agent_key,
        "status": execution.status,
        "trace_id": execution.trace_id,
        "correlation_id": execution.correlation_id,
        "stream_url": f"/api/v1/executions/{execution.id}/stream",
    }


class LifecycleRequest(BaseModel):
    action: str = Field(description="pause|resume|disable|enable")
    reason: str | None = None


@router.post("/{agent_key}/lifecycle")
async def change_lifecycle(
    agent_key: str,
    payload: LifecycleRequest,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    principal.require(Permission.AGENT_LIFECYCLE)
    agent = (
        await session.execute(select(Agent).where(Agent.key == agent_key))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"Agent '{agent_key}' not found")
    mapping = {"pause": "paused", "resume": "active", "enable": "active", "disable": "disabled"}
    if payload.action not in mapping:
        raise ValidationError("Unknown lifecycle action", details={"valid": sorted(mapping)})
    agent.lifecycle_state = mapping[payload.action]
    await write_audit(session, principal=principal, action=f"agent.{payload.action}",
                      resource_type="agent", resource_id=agent_key, severity="warning",
                      details={"reason": payload.reason}, request=request)
    await bus.publish(AGENT_CHANNEL, {"type": "agent.lifecycle", "agent_key": agent_key,
                                      "lifecycle_state": agent.lifecycle_state})
    return {"agent_key": agent_key, "lifecycle_state": agent.lifecycle_state}


class AgentConfigUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=256, le=32000)
    max_iterations: int | None = Field(default=None, ge=1, le=40)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=30)
    tools: list[str] | None = None
    knowledge_sources: list[str] | None = None
    memory_enabled: bool | None = None
    memory_window: int | None = Field(default=None, ge=1, le=100)
    require_citations: bool | None = None
    strict_validation: bool | None = None
    mask_pii: bool | None = None
    blocked_terms: list[str] | None = None
    required_disclaimer: str | None = None
    final_approval_required: bool | None = None
    final_approval_risk_threshold: str | None = None
    approval_role: str | None = None
    cost_cap_usd: float | None = Field(default=None, gt=0, le=100)
    sla_latency_ms: int | None = None
    monthly_budget_usd: float | None = None
    tags: list[str] | None = None
    changelog: str = "Configuration update"


@router.put("/{agent_key}/config")
async def update_agent_config(
    agent_key: str,
    payload: AgentConfigUpdate,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    principal.require(Permission.AGENT_WRITE)
    agent = (
        await session.execute(select(Agent).where(Agent.key == agent_key))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"Agent '{agent_key}' not found")
    if agent.availability != "implemented":
        raise ValidationError("Roadmap agents cannot be configured until implemented")

    updates = payload.model_dump(exclude_none=True, exclude={"changelog"})
    unknown_tools = [t for t in updates.get("tools", []) if not tool_registry.has(t)]
    if unknown_tools:
        raise ValidationError("Unknown tools", details={"unknown": unknown_tools,
                                                        "available": [t.name for t in
                                                                      tool_registry.all()]})
    config = {**(agent.config or {}), **updates}
    agent.config = config
    if "name" in updates:
        agent.name = updates["name"]
    if "description" in updates:
        agent.description = updates["description"]
    if "tools" in updates:
        agent.tools = updates["tools"]
    if "knowledge_sources" in updates:
        agent.knowledge_sources = updates["knowledge_sources"]
    if "tags" in updates:
        agent.tags = updates["tags"]
    if "sla_latency_ms" in updates:
        agent.sla_latency_ms = updates["sla_latency_ms"]
    if "monthly_budget_usd" in updates:
        agent.monthly_budget_usd = updates["monthly_budget_usd"]

    # Take the next unused version number: after a rollback the agent's current version is
    # an older one, so incrementing it would collide with an existing row.
    highest = int(
        (await session.execute(
            select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent.id)
        )).scalar_one() or agent.version
    )
    agent.version = highest + 1
    session.add(AgentVersion(agent_id=agent.id, version=agent.version, config=config,
                             changelog=payload.changelog, published=False))
    agent_registry.apply_override(agent_key, config)
    await write_audit(session, principal=principal, action="agent.config.updated",
                      resource_type="agent", resource_id=agent_key,
                      details={"version": agent.version, "fields": sorted(updates)},
                      request=request)
    return {"agent_key": agent_key, "version": agent.version, "config": config,
            "published": False}


@router.get("/{agent_key}/versions")
async def list_versions(agent_key: str, session: SessionDep,
                        principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.AGENT_READ)
    agent = (
        await session.execute(select(Agent).where(Agent.key == agent_key))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"Agent '{agent_key}' not found")
    versions = (
        await session.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version": v.version, "changelog": v.changelog, "published": v.published,
            "is_current": v.is_current,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "published_by": v.published_by, "created_at": v.created_at.isoformat(),
            "config": v.config,
        }
        for v in versions
    ]


@router.post("/{agent_key}/versions/{version}/publish")
async def publish_version(agent_key: str, version: int, request: Request, session: SessionDep,
                          principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.AGENT_PUBLISH)
    agent = (
        await session.execute(select(Agent).where(Agent.key == agent_key))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"Agent '{agent_key}' not found")
    target = (
        await session.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent.id,
                                       AgentVersion.version == version)
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(f"Version {version} not found for agent '{agent_key}'")
    others = (
        await session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent.id))
    ).scalars().all()
    for v in others:
        v.is_current = v.id == target.id
    target.published = True
    target.published_at = datetime.now(UTC)
    target.published_by = principal.email
    agent.config = target.config
    agent.version = target.version
    agent.tools = target.config.get("tools", agent.tools)
    agent.knowledge_sources = target.config.get("knowledge_sources", agent.knowledge_sources)
    agent_registry.apply_override(agent_key, target.config)
    await write_audit(session, principal=principal, action="agent.version.published",
                      resource_type="agent", resource_id=agent_key, severity="warning",
                      details={"version": version}, request=request)
    return {"agent_key": agent_key, "version": version, "published": True}


@router.post("/{agent_key}/versions/{version}/rollback")
async def rollback_version(agent_key: str, version: int, request: Request, session: SessionDep,
                           principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.AGENT_PUBLISH)
    return await publish_version(agent_key, version, request, session, principal)


class CreateAgentRequest(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str
    description: str = ""
    category: str = "Custom"
    system_prompt: str
    model: str | None = None
    temperature: float = 0.2
    tools: list[str] = Field(default_factory=list)
    knowledge_sources: list[str] = Field(default_factory=list)
    memory_enabled: bool = False
    final_approval_required: bool = False
    cost_cap_usd: float = 2.0
    tags: list[str] = Field(default_factory=list)
    owner: str = "Platform Engineering"
    department: str = "Technology"
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"query": {"type": "string", "required": True, "label": "Request"}}
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(payload: CreateAgentRequest, request: Request, session: SessionDep,
                       principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.AGENT_WRITE)
    existing = (
        await session.execute(select(Agent).where(Agent.key == payload.key))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"Agent '{payload.key}' already exists")
    unknown = [t for t in payload.tools if not tool_registry.has(t)]
    if unknown:
        raise ValidationError("Unknown tools", details={"unknown": unknown})

    from app.agents.base import AgentSpec

    spec = AgentSpec(
        key=payload.key, name=payload.name, description=payload.description,
        category=payload.category, system_prompt=payload.system_prompt, model=payload.model,
        temperature=payload.temperature, tools=payload.tools,
        knowledge_sources=payload.knowledge_sources, memory_enabled=payload.memory_enabled,
        final_approval_required=payload.final_approval_required, cost_cap_usd=payload.cost_cap_usd,
        tags=payload.tags, owner=payload.owner, department=payload.department,
        input_schema=payload.input_schema,
    )
    agent_registry.register_custom(spec)
    config = spec.to_config()
    agent = Agent(
        key=spec.key, name=spec.name, description=spec.description, category=spec.category,
        availability="implemented", lifecycle_state="active", owner=spec.owner,
        department=spec.department, version=1, is_builtin=False, config=config,
        tags=spec.tags, tools=spec.tools, knowledge_sources=spec.knowledge_sources,
        created_by=principal.email,
    )
    session.add(agent)
    await session.flush()
    session.add(AgentVersion(agent_id=agent.id, version=1, config=config,
                             changelog="Initial version", published=True,
                             published_at=datetime.now(UTC), published_by=principal.email,
                             is_current=True))
    await write_audit(session, principal=principal, action="agent.created", resource_type="agent",
                      resource_id=agent.key, severity="warning", request=request)
    return _serialise_agent(agent, {})
