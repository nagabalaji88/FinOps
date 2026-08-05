"""Conformance API: inspect the scenario catalogue and run the validation agent."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, SessionDep, write_audit
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.rbac import Permission
from app.validation import SCENARIOS, ValidationRunner, to_markdown

router = APIRouter(prefix="/validation", tags=["validation"])
log = get_logger("api.validation")

#: Completed and in-flight runs, newest last. Runs are ephemeral by design: the durable
#: record of what happened is the executions and traces the suite produced.
_RUNS: dict[str, dict[str, Any]] = {}
_MAX_RUNS = 25
_lock = asyncio.Lock()


def _serialise_scenario(scenario: Any) -> dict[str, Any]:
    expect = scenario.expect
    return {
        "id": scenario.id,
        "agent_key": scenario.agent_key,
        "title": scenario.title,
        "rationale": scenario.rationale,
        "input": scenario.payload,
        "tags": list(scenario.tags),
        "requires_sample_data": scenario.requires_sample_data,
        "approval_decision": scenario.approval_decision,
        "expectations": {
            "status": expect.status,
            "tools_called": list(expect.tools_called),
            "tools_forbidden": list(expect.tools_forbidden),
            "must_match": list(expect.must_match),
            "must_not_match": list(expect.must_not_match),
            "requires_citations": expect.requires_citations,
            "approval_expected": expect.approval_expected,
            "approval_on_tool": expect.approval_on_tool,
            "guardrail_rules_expected": list(expect.guardrail_rules_expected),
            "max_cost_usd": expect.max_cost_usd,
            "max_latency_ms": expect.max_latency_ms,
        },
    }


@router.get("/scenarios")
async def list_scenarios(principal: PrincipalDep, agent_key: str | None = None,
                         tag: str | None = None) -> dict[str, Any]:
    principal.require(Permission.EVAL_READ)
    scenarios = list(SCENARIOS)
    if agent_key:
        scenarios = [s for s in scenarios if s.agent_key == agent_key]
    if tag:
        scenarios = [s for s in scenarios if tag in s.tags]
    agents: dict[str, int] = {}
    for scenario in scenarios:
        agents[scenario.agent_key] = agents.get(scenario.agent_key, 0) + 1
    return {
        "total": len(scenarios),
        "by_agent": agents,
        "tags": sorted({tag for s in scenarios for tag in s.tags}),
        "scenarios": [_serialise_scenario(s) for s in scenarios],
    }


class RunRequest(BaseModel):
    agent_key: str | None = None
    scenario_ids: list[str] = Field(default_factory=list)
    tag: str | None = None
    concurrency: int = Field(default=1, ge=1, le=8)
    reviewer_email: str = "approver@finops.local"


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def start_run(payload: RunRequest, request: Request, session: SessionDep,
                    principal: PrincipalDep) -> dict[str, Any]:
    """Start a conformance run in the background and return its identifier."""
    principal.require(Permission.EVAL_RUN)

    selected = list(SCENARIOS)
    if payload.agent_key:
        selected = [s for s in selected if s.agent_key == payload.agent_key]
    if payload.scenario_ids:
        wanted = {sid.strip().upper() for sid in payload.scenario_ids}
        selected = [s for s in selected if s.id.upper() in wanted]
    if payload.tag:
        selected = [s for s in selected if payload.tag in s.tags]
    if not selected:
        raise ValidationError("No scenarios matched the filters",
                              details={"available": [s.id for s in SCENARIOS]})

    run_id = uuid.uuid4().hex[:12]
    validation_runner = ValidationRunner(
        reviewer_email=payload.reviewer_email, concurrency=payload.concurrency
    )
    preflight = await validation_runner.preflight(selected)

    async with _lock:
        _RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "requested_by": principal.email,
            "scenario_ids": [s.id for s in selected],
            "preflight": preflight,
            "completed": 0,
            "total": len(selected),
            "results": [],
            "summary": None,
            "error": None,
        }
        while len(_RUNS) > _MAX_RUNS:
            _RUNS.pop(next(iter(_RUNS)))

    async def record(result: Any) -> None:
        async with _lock:
            entry = _RUNS[run_id]
            entry["completed"] += 1
            entry["results"].append(result.to_dict())

    async def execute() -> None:
        try:
            report = await validation_runner.run(selected, on_result=record)
            async with _lock:
                _RUNS[run_id].update(
                    status="completed",
                    finished_at=datetime.now(UTC).isoformat(),
                    summary=report.to_dict()["summary"],
                    by_agent=report.to_dict()["by_agent"],
                    results=report.to_dict()["results"],
                    markdown=to_markdown(report),
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("validation_run_failed", run_id=run_id, error=str(exc))
            async with _lock:
                _RUNS[run_id].update(
                    status="failed",
                    finished_at=datetime.now(UTC).isoformat(),
                    error=f"{type(exc).__name__}: {exc}",
                )

    asyncio.create_task(execute())
    await write_audit(session, principal=principal, action="validation.run.started",
                      resource_type="validation_run", resource_id=run_id,
                      details={"scenarios": len(selected)}, request=request)
    return {
        "run_id": run_id,
        "status": "running",
        "scenarios": len(selected),
        "preflight": preflight,
        "poll_url": f"/api/v1/validation/runs/{run_id}",
    }


@router.get("/runs")
async def list_runs(principal: PrincipalDep) -> list[dict[str, Any]]:
    principal.require(Permission.EVAL_READ)
    return [
        {
            "run_id": run["run_id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "requested_by": run["requested_by"],
            "completed": run["completed"],
            "total": run["total"],
            "summary": run["summary"],
        }
        for run in _RUNS.values()
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: PrincipalDep,
                  include_markdown: bool = False) -> dict[str, Any]:
    principal.require(Permission.EVAL_READ)
    run = _RUNS.get(run_id)
    if run is None:
        raise NotFoundError(f"Validation run '{run_id}' not found")
    payload = {key: value for key, value in run.items() if key != "markdown"}
    if include_markdown and "markdown" in run:
        payload["markdown"] = run["markdown"]
    return payload
