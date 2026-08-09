"""FastAPI application: middleware, lifespan wiring, health probes and metrics."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.bus import bus
from app.core.cache import cache
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_id_ctx, trace_id_ctx
from app.core.metrics import http_request_duration, http_requests_total, render_metrics
from app.core.otel import current_trace_ids, setup_tracing
from app.core.secrets import secret_manager
from app.core.storage import store
from app.db.session import dispose_engine, engine, ping_database
from app.llm.base import close_http_client
from app.llm.router import router as model_router

configure_logging()
log = get_logger("api")

DESCRIPTION = """
Production control plane for enterprise AI agents in financial services.

* **Agents** - catalogue, lifecycle, versioning and execution
* **Executions** - live streaming, OpenTelemetry-style traces, execution DAG, logs
* **Approvals** - human-in-the-loop gates that suspend and resume real executions
* **Cost** - per agent, model, user, department, tool and API, with forecasting
* **Knowledge** - connectors, ingestion, hybrid retrieval with citations
* **Security** - RBAC, API keys, MFA, SSO, secrets and a full audit trail

Every endpoint requires authentication. Capabilities that depend on an external provider
return `503 provider_not_configured` with the exact settings required rather than a
fabricated response.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.rag.embeddings import active_dimensions
    from app.rag.vectorstore import init_vector_store
    from app.services import telemetry
    from app.tools import registry as tool_registry  # noqa: F401 - registers tools

    setup_tracing(app, engine)
    await cache.connect()
    await bus.connect()
    await store.connect()
    await secret_manager.connect()
    await init_vector_store(active_dimensions())
    telemetry.install()

    if settings.environment in {"local", "dev"}:
        from app.db.session import session_scope
        from app.services.bootstrap import bootstrap, ensure_schema

        await ensure_schema()
        async with session_scope() as session:
            await bootstrap(session)

    log.info(
        "startup_complete",
        environment=settings.environment,
        providers=model_router.configured_providers(),
        cache=cache.backend,
        storage=store.backend,
        secrets=secret_manager.backend,
    )
    yield
    await bus.close()
    await cache.close()
    await close_http_client()
    await dispose_engine()
    log.info("shutdown_complete")


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    root_path=settings.root_path,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Trace-ID", "X-Response-Time-Ms"],
)
if settings.environment == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request_id_ctx.set(request_id)
    trace_id, _ = current_trace_ids()
    if trace_id:
        trace_id_ctx.set(trace_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        http_requests_total.labels(request.method, path, "500").inc()
        http_request_duration.labels(request.method, path).observe(duration)
        raise
    duration = time.perf_counter() - started
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    http_requests_total.labels(request.method, path, str(response.status_code)).inc()
    http_request_duration.labels(request.method, path).observe(duration)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{duration * 1000:.2f}"
    if trace_id:
        response.headers["X-Trace-ID"] = trace_id
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log.warning("app_error", code=exc.code, message=exc.message, path=request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            "request_id": request_id_ctx.get(),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "request_validation_error",
                "message": "Invalid request",
                "details": {"errors": exc.errors()[:20]},
            },
            "request_id": request_id_ctx.get(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred",
                "details": {"type": type(exc).__name__} if settings.debug else {},
            },
            "request_id": request_id_ctx.get(),
        },
    )


app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, Any]:
    return {"status": "alive", "service": settings.otel_service_name, "version": "1.0.0"}


@app.get("/health/ready", tags=["health"])
async def readiness() -> Response:
    database_ok = await ping_database()
    providers = model_router.configured_providers()
    checks = {
        "database": "ok" if database_ok else "unavailable",
        "cache": cache.backend,
        "event_bus": "kafka" if bus.kafka_connected else "in_process",
        "artifact_store": store.backend,
        "secrets": secret_manager.backend,
        "llm_providers": providers or "none_configured",
    }
    ready = database_ok
    body = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "warnings": []
        if providers
        else [
            "No LLM provider configured - agent executions will fail with "
            "provider_not_configured until one is set"
        ],
    }
    return JSONResponse(content=body, status_code=200 if ready else 503)


@app.get("/health/startup", tags=["health"])
async def startup_probe() -> dict[str, Any]:
    return {"status": "started"}


@app.get("/metrics", tags=["health"], response_class=PlainTextResponse)
async def metrics() -> Response:
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/", tags=["health"])
async def root() -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": "1.0.0",
        "environment": settings.environment,
        "docs": "/docs",
        "api": settings.api_prefix,
        "health": "/health/ready",
        "metrics": "/metrics",
    }
