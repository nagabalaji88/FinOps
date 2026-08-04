"""Tools API, connected services health, knowledge administration, playground and evaluations."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.bus import bus
from app.core.cache import cache
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.rbac import Permission
from app.core.resilience import all_breakers
from app.core.secrets import secret_manager
from app.core.storage import store
from app.db.models.agents import Evaluation, Execution, PlaygroundRun, ToolHealth
from app.db.models.knowledge import Document, KnowledgeSource
from app.db.session import ping_database
from app.llm.catalog import CATALOG, compute_cost, resolve_model
from app.llm.router import router as model_router
from app.llm.types import Message
from app.rag.connectors import CONNECTORS
from app.rag.pipeline import pipeline
from app.rag.vectorstore import get_vector_store
from app.tools.base import ToolContext
from app.tools.base import registry as tool_registry

tools_router = APIRouter(prefix="/tools", tags=["tools"])
services_router = APIRouter(prefix="/services", tags=["services"])
knowledge_router = APIRouter(prefix="/knowledge", tags=["knowledge"])
playground_router = APIRouter(prefix="/playground", tags=["playground"])
evals_router = APIRouter(prefix="/evaluations", tags=["evaluations"])


# --- tools -------------------------------------------------------------------
@tools_router.get("")
async def list_tools(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.TOOL_READ)
    health_rows = {
        h.tool_name: h
        for h in (await session.execute(select(ToolHealth))).scalars().all()
    }
    out = []
    for tool in tool_registry.all():
        health = health_rows.get(tool.name)
        out.append({
            "name": tool.name,
            "description": tool.description,
            "category": tool.category,
            "requires_approval": tool.requires_approval,
            "approval_risk": tool.approval_risk,
            "writes_data": tool.writes_data,
            "idempotent": tool.idempotent,
            "timeout_seconds": tool.timeout_seconds,
            "max_retries": tool.max_retries,
            "schema": tool.json_schema,
            "health": {
                "status": health.last_status if health else "unknown",
                "total_calls": health.total_calls if health else 0,
                "failures": health.total_failures if health else 0,
                "timeouts": health.total_timeouts if health else 0,
                "retries": health.total_retries if health else 0,
                "p50_latency_ms": round(health.p50_latency_ms, 2) if health else 0.0,
                "p95_latency_ms": round(health.p95_latency_ms, 2) if health else 0.0,
                "success_rate_pct": round(
                    (health.total_calls - health.total_failures) / health.total_calls * 100, 2)
                if health and health.total_calls else 100.0,
                "last_called_at": health.last_called_at.isoformat()
                if health and health.last_called_at else None,
                "last_error": health.last_error if health else None,
            },
        })
    return out


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@tools_router.post("/{tool_name}/invoke")
async def invoke_tool(tool_name: str, payload: ToolInvokeRequest, request: Request,
                      session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.TOOL_INVOKE)
    tool = tool_registry.get(tool_name)
    if tool.requires_approval:
        raise ValidationError(
            f"Tool '{tool_name}' requires human approval and can only run inside an agent "
            f"execution where the approval gate is enforced",
        )
    result = await tool_registry.invoke(
        tool_name, payload.arguments,
        ToolContext(user_id=principal.id, user_email=principal.email, roles=principal.roles,
                    session=session, state={}),
    )
    await write_audit(session, principal=principal, action="tool.invoked", resource_type="tool",
                      resource_id=tool_name, outcome="success" if result.ok else "failure",
                      details={"arguments": payload.arguments}, request=request)
    return result.to_dict()


# --- connected services -------------------------------------------------------
async def _probe(name: str, category: str, coro) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await coro
        latency = (time.perf_counter() - started) * 1000
        if isinstance(result, dict):
            return {"name": name, "category": category, "latency_ms": round(latency, 2), **result}
        return {"name": name, "category": category, "status": "healthy" if result
                else "not_configured", "latency_ms": round(latency, 2)}
    except Exception as exc:
        return {"name": name, "category": category, "status": "unhealthy", "error": str(exc)[:300],
                "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


async def _redis_status() -> dict[str, Any]:
    if not settings.redis_url:
        return {"status": "not_configured", "required": ["REDIS_URL"]}
    ok = await cache.ping()
    return {"status": "healthy" if ok else "unhealthy", "backend": cache.backend}


async def _kafka_status() -> dict[str, Any]:
    if not settings.kafka_bootstrap_servers:
        return {"status": "not_configured", "required": ["KAFKA_BOOTSTRAP_SERVERS"]}
    return {"status": "healthy" if bus.kafka_connected else "unhealthy",
            "brokers": settings.kafka_bootstrap_servers}


async def _postgres_status() -> dict[str, Any]:
    ok = await ping_database()
    dialect = "postgresql" if not settings.is_sqlite else "sqlite"
    return {"status": "healthy" if ok else "unhealthy", "dialect": dialect}


async def _qdrant_status() -> dict[str, Any]:
    if not settings.qdrant_url:
        return {"status": "not_configured", "required": ["QDRANT_URL"],
                "fallback": "exact cosine search in the primary database"}
    return await get_vector_store().health()


async def _neo4j_status() -> dict[str, Any]:
    if not (settings.neo4j_uri and settings.neo4j_user):
        return {"status": "not_configured", "required": ["NEO4J_URI", "NEO4J_USER",
                                                         "NEO4J_PASSWORD"]}
    try:
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(settings.neo4j_uri,
                                           auth=(settings.neo4j_user, settings.neo4j_password))
        await driver.verify_connectivity()
        await driver.close()
        return {"status": "healthy"}
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)[:200]}


async def _elastic_status() -> dict[str, Any]:
    if not settings.elasticsearch_url:
        return {"status": "not_configured", "required": ["ELASTICSEARCH_URL"]}
    from app.llm.base import http_client

    resp = await http_client().get(settings.elasticsearch_url, timeout=8.0)
    return {"status": "healthy" if resp.status_code < 400 else "unhealthy",
            "http_status": resp.status_code}


async def _http_probe(name: str, url: str | None, required: list[str]) -> dict[str, Any]:
    if not url:
        return {"status": "not_configured", "required": required}
    from app.llm.base import http_client

    resp = await http_client().get(url, timeout=8.0)
    return {"status": "healthy" if resp.status_code < 500 else "unhealthy",
            "http_status": resp.status_code, "url": url}


@services_router.get("")
async def connected_services(principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.SERVICE_READ)
    provider_health = await model_router.health()
    providers = [
        {"name": p["provider"], "category": "ai_provider",
         "status": p.get("status", "not_configured" if not p["configured"] else "unknown"),
         "latency_ms": p.get("latency_ms"), "error": p.get("error"),
         "configured": p["configured"],
         "models": [m.id for m in CATALOG.values() if m.provider == p["provider"]]}
        for p in provider_health
    ]
    infra = await asyncio.gather(
        _probe("postgresql", "datastore", _postgres_status()),
        _probe("redis", "cache", _redis_status()),
        _probe("kafka", "streaming", _kafka_status()),
        _probe("qdrant", "vector", _qdrant_status()),
        _probe("neo4j", "graph", _neo4j_status()),
        _probe("elasticsearch", "search", _elastic_status()),
        _probe("minio", "object_store", asyncio.sleep(0, result={
            "status": "healthy" if store.healthy else "not_configured",
            "backend": store.backend,
            "required": [] if store.healthy else ["MINIO_ENDPOINT", "MINIO_ACCESS_KEY",
                                                  "MINIO_SECRET_KEY"]})),
        _probe("vault", "secrets", asyncio.sleep(0, result={
            "status": "healthy" if secret_manager.healthy else "not_configured",
            "backend": secret_manager.backend,
            "required": [] if secret_manager.healthy else ["VAULT_ADDR", "VAULT_TOKEN"]})),
        _probe("keycloak", "identity", _http_probe(
            "keycloak",
            f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"
            if settings.keycloak_url and settings.keycloak_realm else None,
            ["KEYCLOAK_URL", "KEYCLOAK_REALM"])),
        _probe("prometheus", "observability", _http_probe(
            "prometheus", None, ["scrape /metrics from this service"])),
        _probe("jaeger", "observability", asyncio.sleep(0, result={
            "status": "healthy" if settings.otel_exporter_otlp_endpoint else "not_configured",
            "endpoint": settings.otel_exporter_otlp_endpoint,
            "required": [] if settings.otel_exporter_otlp_endpoint
            else ["OTEL_EXPORTER_OTLP_ENDPOINT"]})),
        _probe("temporal", "orchestration", asyncio.sleep(0, result={
            "status": "healthy" if settings.temporal_host else "not_configured",
            "host": settings.temporal_host, "namespace": settings.temporal_namespace,
            "required": [] if settings.temporal_host else ["TEMPORAL_HOST"]})),
        _probe("celery", "workers", asyncio.sleep(0, result={
            "status": "healthy" if settings.celery_broker_url else "not_configured",
            "broker": settings.celery_broker_url,
            "required": [] if settings.celery_broker_url else ["CELERY_BROKER_URL"]})),
    )
    connectors = [
        {"name": c.key, "category": "knowledge_connector", "label": c.label,
         "status": "configured" if c.configured else "not_configured",
         "required": c.missing()}
        for c in CONNECTORS.values()
    ]
    all_services = providers + list(infra) + connectors
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total": len(all_services),
            "healthy": sum(1 for s in all_services if s.get("status") in {"healthy", "configured"}),
            "not_configured": sum(1 for s in all_services if s.get("status") == "not_configured"),
            "unhealthy": sum(1 for s in all_services
                             if s.get("status") in {"unhealthy", "degraded"}),
        },
        "services": all_services,
        "circuit_breakers": all_breakers(),
    }


# --- knowledge administration --------------------------------------------------
@knowledge_router.get("/sources")
async def list_sources(session: SessionDep, principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.KNOWLEDGE_READ)
    rows = (await session.execute(select(KnowledgeSource).order_by(KnowledgeSource.name))) \
        .scalars().all()
    return [
        {"id": s.id, "key": s.key, "name": s.name, "connector": s.connector, "status": s.status,
         "enabled": s.enabled, "documents": s.document_count, "chunks": s.chunk_count,
         "embedding_model": s.embedding_model, "vector_dimensions": s.vector_dimensions,
         "classification": s.classification, "config": s.config, "last_error": s.last_error,
         "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None}
        for s in rows
    ]


class SourceCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str
    connector: str
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    classification: str = "internal"


@knowledge_router.post("/sources", status_code=status.HTTP_201_CREATED)
async def create_source(payload: SourceCreate, session: SessionDep,
                        principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.KNOWLEDGE_WRITE)
    if payload.connector not in CONNECTORS:
        raise ValidationError("Unknown connector", details={"available": sorted(CONNECTORS)})
    source = KnowledgeSource(
        key=payload.key, name=payload.name, connector=payload.connector,
        description=payload.description, config=payload.config,
        classification=payload.classification,
        status="connected" if payload.connector == "upload" else "not_configured",
    )
    session.add(source)
    await session.flush()
    await write_audit(session, principal=principal, action="knowledge.source.created",
                      resource_type="knowledge_source", resource_id=source.key)
    return {"id": source.id, "key": source.key, "status": source.status}


@knowledge_router.post("/sources/{key}/sync")
async def sync_source(key: str, session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.KNOWLEDGE_WRITE)
    source = (
        await session.execute(select(KnowledgeSource).where(KnowledgeSource.key == key))
    ).scalar_one_or_none()
    if source is None:
        raise NotFoundError(f"Knowledge source '{key}' not found")
    connector = CONNECTORS.get(source.connector)
    if connector is None:
        raise ValidationError(f"No connector implementation for '{source.connector}'")
    result = await connector.sync(session, source)
    await write_audit(session, principal=principal, action="knowledge.source.synced",
                      resource_type="knowledge_source", resource_id=key, details=result)
    return result


@knowledge_router.post("/sources/{key}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    key: str,
    session: SessionDep,
    principal: PrincipalDep,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    classification: str = Form(default="internal"),
) -> dict[str, Any]:
    principal.require(Permission.KNOWLEDGE_WRITE)
    source = (
        await session.execute(select(KnowledgeSource).where(KnowledgeSource.key == key))
    ).scalar_one_or_none()
    if source is None:
        raise NotFoundError(f"Knowledge source '{key}' not found")
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise ValidationError("File exceeds the 25 MB upload limit")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        if file.content_type == "application/pdf":
            try:
                import io

                import pypdfium2 as pdfium

                pdf = pdfium.PdfDocument(io.BytesIO(raw))
                content = "\n\n".join(
                    pdf[i].get_textpage().get_text_range() for i in range(len(pdf))
                )
            except Exception as exc:
                raise ValidationError(f"Unable to extract text from PDF: {exc}") from exc
        else:
            raise ValidationError("Only UTF-8 text or PDF uploads are supported")

    stored = await store.put(f"knowledge/{key}/{file.filename}", raw,
                             file.content_type or "text/plain")
    document = await pipeline.ingest_document(
        session, source=source, title=title or file.filename or "Untitled", content=content,
        uri=stored["uri"], external_id=file.filename,
        mime_type=file.content_type or "text/plain", author=principal.email,
        classification=classification, metadata={"sha256": stored["sha256"],
                                                 "size_bytes": stored["size_bytes"]},
    )
    document.artifact_uri = stored["uri"]
    await write_audit(session, principal=principal, action="knowledge.document.uploaded",
                      resource_type="document", resource_id=document.id,
                      details={"source": key, "filename": file.filename})
    return {"document_id": document.id, "title": document.title,
            "chunks": document.chunk_count, "embedding_status": document.embedding_status,
            "artifact": stored}


@knowledge_router.get("/documents")
async def list_documents(session: SessionDep, principal: PrincipalDep,
                         source_key: str | None = None,
                         limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    principal.require(Permission.KNOWLEDGE_READ)
    stmt = select(Document).order_by(Document.updated_at.desc()).limit(limit)
    if source_key:
        source = (
            await session.execute(select(KnowledgeSource).where(KnowledgeSource.key == source_key))
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError(f"Knowledge source '{source_key}' not found")
        stmt = stmt.where(Document.source_id == source.id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {"id": d.id, "title": d.title, "uri": d.uri, "author": d.author,
         "classification": d.classification, "chunk_count": d.chunk_count,
         "token_count": d.token_count, "embedding_status": d.embedding_status,
         "version": d.version, "updated_at": d.updated_at.isoformat()}
        for d in rows
    ]


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=6, ge=1, le=25)
    sources: list[str] = Field(default_factory=list)


@knowledge_router.post("/search")
async def search_knowledge(payload: SearchRequest, session: SessionDep,
                           principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.KNOWLEDGE_READ)
    result = await pipeline.search(session, payload.query, top_k=payload.top_k,
                                   source_keys=payload.sources or None)
    return {
        "query": payload.query, "backend": result.backend, "method": result.method,
        "embedding_model": result.embedding_model, "latency_ms": round(result.latency_ms, 2),
        "results": [m.to_dict() for m in result.matches],
        "citations": result.to_citations(),
    }


# --- playground ---------------------------------------------------------------
class PlaygroundRequest(BaseModel):
    prompt: str
    system_prompt: str | None = None
    models: list[str] = Field(min_length=1, max_length=6)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=16, le=16000)
    json_mode: bool = False


@playground_router.post("/run")
async def run_playground(payload: PlaygroundRequest, session: SessionDep,
                         principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.PLAYGROUND_USE)
    messages = []
    if payload.system_prompt:
        messages.append(Message(role="system", content=payload.system_prompt))
    messages.append(Message(role="user", content=payload.prompt))

    async def run_model(model_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = await model_router.chat(
                messages=messages, model=model_id, temperature=payload.temperature,
                max_tokens=payload.max_tokens, json_mode=payload.json_mode,
                allow_fallback=False,
                context={"user_email": principal.email, "department": principal.department,
                         "agent_key": "playground"},
            )
            return {
                "model": model_id, "ok": True, "content": response.content,
                "provider": response.provider,
                "latency_ms": round(response.latency_ms, 2),
                "tokens": {"input": response.usage.input_tokens,
                           "output": response.usage.output_tokens},
                "cost_usd": round(response.cost_usd, 6),
                "finish_reason": response.finish_reason,
                "characters": len(response.content),
            }
        except Exception as exc:
            return {"model": model_id, "ok": False, "error": str(exc)[:600],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}

    results = list(await asyncio.gather(*(run_model(m) for m in payload.models)))
    total_cost = sum(r.get("cost_usd", 0.0) for r in results)
    run = PlaygroundRun(
        prompt=payload.prompt, system_prompt=payload.system_prompt, models=payload.models,
        parameters={"temperature": payload.temperature, "max_tokens": payload.max_tokens,
                    "json_mode": payload.json_mode},
        results=results, total_cost_usd=total_cost, user_email=principal.email,
    )
    session.add(run)
    await session.flush()
    successful = [r for r in results if r["ok"]]
    return {
        "run_id": run.id,
        "results": results,
        "total_cost_usd": round(total_cost, 6),
        "comparison": {
            "fastest": min(successful, key=lambda r: r["latency_ms"])["model"]
            if successful else None,
            "cheapest": min(successful, key=lambda r: r["cost_usd"])["model"]
            if successful else None,
            "longest_output": max(successful, key=lambda r: r["characters"])["model"]
            if successful else None,
        },
    }


@playground_router.get("/runs")
async def list_playground_runs(session: SessionDep, principal: PrincipalDep,
                               limit: int = Query(default=25, ge=1, le=200)) -> list[dict[str, Any]]:
    principal.require(Permission.PLAYGROUND_USE)
    rows = (
        await session.execute(
            select(PlaygroundRun).order_by(PlaygroundRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        {"id": r.id, "prompt": r.prompt[:400], "models": r.models,
         "parameters": r.parameters, "total_cost_usd": round(r.total_cost_usd, 6),
         "user_email": r.user_email, "created_at": r.created_at.isoformat(),
         "results": r.results}
        for r in rows
    ]


@playground_router.get("/models")
async def available_models(principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.PLAYGROUND_USE)
    configured = set(model_router.configured_providers())
    return {
        "configured_providers": sorted(configured),
        "models": [
            {
                "id": spec.id, "display_name": spec.display_name, "provider": spec.provider,
                "family": spec.family, "tier": spec.tier,
                "context_window": spec.context_window,
                "max_output_tokens": spec.max_output_tokens,
                "input_price_per_mtok": spec.input_price_per_mtok,
                "output_price_per_mtok": spec.output_price_per_mtok,
                "supports_tools": spec.supports_tools, "supports_vision": spec.supports_vision,
                "is_embedding": spec.is_embedding, "available": spec.provider in configured,
                "tags": list(spec.tags),
            }
            for spec in CATALOG.values()
        ],
    }


@playground_router.post("/estimate")
async def estimate_cost(principal: PrincipalDep, model: str = Query(...),
                        input_tokens: int = Query(ge=0), output_tokens: int = Query(ge=0)
                        ) -> dict[str, Any]:
    principal.require(Permission.PLAYGROUND_USE)
    spec = resolve_model(model)
    if spec is None:
        raise NotFoundError(f"Unknown model '{model}'")
    return {"model": spec.id, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "estimated_cost_usd": compute_cost(spec, input_tokens, output_tokens)}


# --- evaluations ---------------------------------------------------------------
class FeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    feedback: str | None = None


@evals_router.post("/{execution_id}/feedback")
async def submit_feedback(execution_id: str, payload: FeedbackRequest, session: SessionDep,
                          principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.EVAL_RUN)
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    evaluation = (
        await session.execute(
            select(Evaluation).where(Evaluation.execution_id == execution_id,
                                     Evaluation.evaluator == "human")
        )
    ).scalar_one_or_none()
    if evaluation is None:
        evaluation = Evaluation(execution_id=execution_id, agent_key=execution.agent_key,
                                evaluator="human")
        session.add(evaluation)
    evaluation.human_rating = payload.rating
    evaluation.human_feedback = payload.feedback
    evaluation.reviewer_email = principal.email
    evaluation.latency_ms = execution.latency_ms
    evaluation.cost_usd = execution.cost_usd
    await session.flush()
    return {"execution_id": execution_id, "rating": payload.rating, "recorded": True}


@evals_router.post("/{execution_id}/run")
async def run_evaluation(execution_id: str, session: SessionDep,
                         principal: PrincipalDep) -> dict[str, Any]:
    """Compute automatic quality metrics for a completed execution."""
    principal.require(Permission.EVAL_RUN)
    from app.services.evaluation import evaluate_execution

    return await evaluate_execution(session, execution_id)


@evals_router.get("")
async def list_evaluations(session: SessionDep, principal: PrincipalDep,
                           agent_key: str | None = None,
                           limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    principal.require(Permission.EVAL_READ)
    stmt = select(Evaluation).order_by(Evaluation.created_at.desc()).limit(limit)
    if agent_key:
        stmt = stmt.where(Evaluation.agent_key == agent_key)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {"id": e.id, "execution_id": e.execution_id, "agent_key": e.agent_key,
         "evaluator": e.evaluator, "faithfulness": e.faithfulness,
         "groundedness": e.groundedness, "hallucination_score": e.hallucination_score,
         "citation_score": e.citation_score, "tool_success_rate": e.tool_success_rate,
         "answer_relevance": e.answer_relevance, "human_rating": e.human_rating,
         "human_feedback": e.human_feedback, "latency_ms": e.latency_ms,
         "cost_usd": e.cost_usd, "details": e.details,
         "created_at": e.created_at.isoformat()}
        for e in rows
    ]


@evals_router.get("/summary")
async def evaluation_summary(session: SessionDep, principal: PrincipalDep) -> dict[str, Any]:
    principal.require(Permission.EVAL_READ)
    row = (
        await session.execute(
            select(
                func.count(Evaluation.id), func.avg(Evaluation.faithfulness),
                func.avg(Evaluation.groundedness), func.avg(Evaluation.hallucination_score),
                func.avg(Evaluation.citation_score), func.avg(Evaluation.tool_success_rate),
                func.avg(Evaluation.human_rating),
            )
        )
    ).one()
    return {
        "evaluations": int(row[0] or 0),
        "faithfulness": round(float(row[1] or 0), 4),
        "groundedness": round(float(row[2] or 0), 4),
        "hallucination_score": round(float(row[3] or 0), 4),
        "citation_score": round(float(row[4] or 0), 4),
        "tool_success_rate": round(float(row[5] or 0), 4),
        "human_rating": round(float(row[6] or 0), 2) if row[6] else None,
    }
