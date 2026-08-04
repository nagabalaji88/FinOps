"""Aggregated v1 API router."""

from fastapi import APIRouter

from app.api.v1 import agents, auth, dashboard, executions, operations, platform

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(agents.router)
api_router.include_router(executions.router)
api_router.include_router(dashboard.dashboard_router)
api_router.include_router(dashboard.cost_router)
api_router.include_router(dashboard.metrics_router)
api_router.include_router(operations.approvals_router)
api_router.include_router(operations.logs_router)
api_router.include_router(operations.security_router)
api_router.include_router(platform.tools_router)
api_router.include_router(platform.services_router)
api_router.include_router(platform.knowledge_router)
api_router.include_router(platform.playground_router)
api_router.include_router(platform.evals_router)
