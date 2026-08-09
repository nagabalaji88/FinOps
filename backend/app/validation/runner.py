"""Execution validation agent.

Drives the platform the way an operator would: it routes each scenario to the agent that
owns it, executes it for real through the engine, acts as the human reviewer when a run
suspends for approval, reads back the execution, trace, events and approvals, and asserts
the expected behaviour.

It is not an LLM agent. It is a deterministic conformance driver, so its verdict is
reproducible and can gate a release.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.models.agents import Agent, Approval, Execution, ExecutionEvent, Span
from app.db.models.banking import Customer
from app.db.models.identity import User
from app.db.session import session_scope
from app.engine.executor import engine
from app.llm.router import router as model_router
from app.validation.checks import CheckResult, ObservedExecution, evaluate
from app.validation.scenarios import SCENARIOS, Scenario

log = get_logger("validation")

Verdict = Literal["passed", "failed", "blocked", "error"]

TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}
PROVIDER_ERRORS = {"ProviderNotConfiguredError", "ProviderError", "CircuitOpenError"}

#: Guardrail findings that mean the *rail* could not run, not that the agent misbehaved.
#: Rails fail closed, so an unreachable provider turns into a refusal on every request until
#: the circuit opens and the deterministic rails take over alone. Reporting that as a failed
#: scenario points the operator at the agent instead of at the credential that is wrong.
RAIL_INFRASTRUCTURE_RULES = {"rail_error", "rails_unavailable"}


@dataclass
class ScenarioResult:
    scenario: Scenario
    verdict: Verdict
    checks: list[CheckResult] = field(default_factory=list)
    observed: ObservedExecution | None = None
    duration_ms: int = 0
    note: str = ""

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.failed and c.critical]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.failed and not c.critical]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario.id,
            "agent_key": self.scenario.agent_key,
            "title": self.scenario.title,
            "rationale": self.scenario.rationale,
            "tags": list(self.scenario.tags),
            "input": self.scenario.payload,
            "verdict": self.verdict,
            "note": self.note,
            "duration_ms": self.duration_ms,
            "checks": [c.to_dict() for c in self.checks],
            "execution": None
            if self.observed is None
            else {
                "execution_id": self.observed.execution_id,
                "trace_id": self.observed.trace_id,
                "status": self.observed.status,
                "error": self.observed.error,
                "error_type": self.observed.error_type,
                "cost_usd": round(self.observed.cost_usd, 6),
                "latency_ms": self.observed.latency_ms,
                "tokens": self.observed.tokens,
                "llm_calls": self.observed.llm_calls,
                "node_path": self.observed.node_path,
                "tools": [{"tool": name, "ok": ok} for name, ok in self.observed.tool_invocations],
                "citations": len(self.observed.citations),
                "approvals": len(self.observed.approvals),
                "span_count": self.observed.span_count,
                "response": (self.observed.final_response or "")[:2000],
            },
        }


@dataclass
class ValidationReport:
    started_at: datetime
    finished_at: datetime
    results: list[ScenarioResult]
    preflight: dict[str, Any]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.verdict == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.verdict == "failed")

    @property
    def blocked(self) -> int:
        return sum(1 for r in self.results if r.verdict == "blocked")

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.verdict == "error")

    @property
    def total_cost_usd(self) -> float:
        return sum(r.observed.cost_usd for r in self.results if r.observed)

    @property
    def ok(self) -> bool:
        """A run is acceptable when nothing failed or errored. Blocked is inconclusive."""
        return self.failed == 0 and self.errored == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round((self.finished_at - self.started_at).total_seconds(), 2),
            "preflight": self.preflight,
            "summary": {
                "total": len(self.results),
                "passed": self.passed,
                "failed": self.failed,
                "blocked": self.blocked,
                "errored": self.errored,
                "total_cost_usd": round(self.total_cost_usd, 6),
                "acceptable": self.ok,
            },
            "by_agent": self._by_agent(),
            "results": [r.to_dict() for r in self.results],
        }

    def _by_agent(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for result in self.results:
            bucket = out.setdefault(
                result.scenario.agent_key,
                {"total": 0, "passed": 0, "failed": 0, "blocked": 0, "errored": 0},
            )
            bucket["total"] += 1
            bucket[
                {"passed": "passed", "failed": "failed", "blocked": "blocked", "error": "errored"}[
                    result.verdict
                ]
            ] += 1
        return out


async def prepare_environment(scenarios: list[Scenario], *, seed_sample: bool = True) -> dict[str, Any]:
    """Make the platform ready to run the suite, doing only what is still missing.

    A conformance run should be one command. This creates the schema, seeds the platform
    (identities, agent registry, knowledge corpus, watchlists) and loads the sample banking
    dataset when scenarios need it. Every step is idempotent, so running it against an
    already-prepared deployment is a no-op that costs one query.
    """
    from app.services.bootstrap import bootstrap, ensure_schema, seed_sample_banking

    steps: list[dict[str, Any]] = []

    await ensure_schema()
    steps.append({"step": "schema", "action": "ensured"})

    async with session_scope() as session:
        registered = int((await session.execute(select(func.count(Agent.id)))).scalar_one())
        if registered == 0:
            result = await bootstrap(session)
            steps.append({"step": "platform_seed", "action": "seeded", **result})
        else:
            steps.append({"step": "platform_seed", "action": "already_present", "agents": registered})

    needs_sample = any(s.requires_sample_data for s in scenarios)
    if needs_sample and seed_sample:
        async with session_scope() as session:
            customers = int((await session.execute(select(func.count(Customer.id)))).scalar_one())
            if customers == 0:
                result = await seed_sample_banking(session)
                steps.append({"step": "sample_banking", "action": "seeded", **result})
            else:
                steps.append({"step": "sample_banking", "action": "already_present", "customers": customers})
    elif needs_sample:
        steps.append({"step": "sample_banking", "action": "skipped"})

    return {"prepared": True, "steps": steps}


class ValidationRunner:
    """Executes scenarios against the live platform and validates what comes back."""

    def __init__(
        self,
        *,
        reviewer_email: str = "approver@finops.local",
        execution_timeout: float = 300.0,
        poll_interval: float = 0.5,
        concurrency: int = 1,
    ):
        self.reviewer_email = reviewer_email
        self.execution_timeout = execution_timeout
        self.poll_interval = poll_interval
        self.concurrency = max(1, concurrency)

    # ------------------------------------------------------------------ #
    # Preflight                                                          #
    # ------------------------------------------------------------------ #
    async def preflight(self, scenarios: list[Scenario]) -> dict[str, Any]:
        providers = model_router.configured_providers()
        async with session_scope() as session:
            agents = {a.key: a for a in (await session.execute(select(Agent))).scalars().all()}
            customers = int((await session.execute(select(func.count(Customer.id)))).scalar_one())
            reviewer = (
                await session.execute(select(User).where(User.email == self.reviewer_email))
            ).scalar_one_or_none()

        required_agents = sorted({s.agent_key for s in scenarios})
        missing = [key for key in required_agents if key not in agents]
        inactive = [
            key for key in required_agents if key in agents and agents[key].lifecycle_state != "active"
        ]
        needs_sample = any(s.requires_sample_data for s in scenarios)

        report: dict[str, Any] = {
            "configured_providers": providers,
            "llm_available": bool(providers),
            "agents_required": required_agents,
            "agents_missing": missing,
            "agents_not_active": inactive,
            "sample_banking_data_required": needs_sample,
            "sample_banking_customers": customers,
            "reviewer_present": reviewer is not None,
            "reviewer_email": self.reviewer_email,
            "warnings": [],
        }
        warnings: list[str] = report["warnings"]
        if not providers:
            warnings.append(
                "No LLM provider is configured. Agent executions will fail with "
                "provider_not_configured and every scenario will report BLOCKED."
            )
        if needs_sample and customers == 0:
            warnings.append(
                "Scenarios require the sample banking dataset. Run `python -m app.cli seed-banking` first."
            )
        if missing:
            warnings.append(f"Agents not registered: {missing}")
        if inactive:
            warnings.append(f"Agents not active: {inactive}")
        if reviewer is None:
            warnings.append(
                f"Reviewer '{self.reviewer_email}' not found; approval scenarios cannot be decided."
            )
        return report

    # ------------------------------------------------------------------ #
    # Execution                                                          #
    # ------------------------------------------------------------------ #
    async def run(
        self,
        scenarios: list[Scenario] | None = None,
        *,
        on_result: Any = None,
    ) -> ValidationReport:
        selected = scenarios if scenarios is not None else list(SCENARIOS)
        started = datetime.now(UTC)
        preflight = await self.preflight(selected)

        results: list[ScenarioResult] = []
        if self.concurrency == 1:
            for scenario in selected:
                result = await self.run_scenario(scenario)
                results.append(result)
                if on_result:
                    maybe = on_result(result)
                    if asyncio.iscoroutine(maybe):
                        await maybe
        else:
            semaphore = asyncio.Semaphore(self.concurrency)

            async def guarded(scenario: Scenario) -> ScenarioResult:
                async with semaphore:
                    return await self.run_scenario(scenario)

            gathered = await asyncio.gather(*(guarded(s) for s in selected))
            results = list(gathered)
            if on_result:
                for result in results:
                    maybe = on_result(result)
                    if asyncio.iscoroutine(maybe):
                        await maybe

        return ValidationReport(
            started_at=started,
            finished_at=datetime.now(UTC),
            results=results,
            preflight=preflight,
        )

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        started = time.perf_counter()
        log.info("scenario_started", scenario=scenario.id, agent=scenario.agent_key)
        try:
            execution_id = await self._submit(scenario)
        except Exception as exc:
            return ScenarioResult(
                scenario=scenario,
                verdict="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                note=f"could not submit: {type(exc).__name__}: {exc}",
            )

        try:
            await self._drive_to_terminal(execution_id, scenario)
            observed = await self._observe(execution_id)
        except TimeoutError:
            return ScenarioResult(
                scenario=scenario,
                verdict="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                note=f"execution {execution_id} did not reach a terminal state within "
                f"{self.execution_timeout:.0f}s",
            )
        except Exception as exc:
            return ScenarioResult(
                scenario=scenario,
                verdict="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                note=f"validator error: {type(exc).__name__}: {exc}",
            )

        duration_ms = int((time.perf_counter() - started) * 1000)

        # A missing provider is an environment gap, not an agent defect. Say so rather
        # than reporting a failure the operator cannot act on.
        if observed.status == "failed" and observed.error_type in PROVIDER_ERRORS:
            return ScenarioResult(
                scenario=scenario,
                verdict="blocked",
                observed=observed,
                duration_ms=duration_ms,
                note=f"{observed.error_type}: {observed.error}",
            )

        # The same gap wearing a different hat: the rail itself could not reach the provider
        # and refused the request. Only when the scenario did not *expect* a refusal — a
        # scenario proving a rail fires is still meaningful, and must be judged normally.
        if observed.status == "failed" and scenario.expect.status != "failed":
            infra = next(
                (f for f in observed.guardrail_findings if str(f.get("rule")) in RAIL_INFRASTRUCTURE_RULES),
                None,
            )
            if infra is not None:
                return ScenarioResult(
                    scenario=scenario,
                    verdict="blocked",
                    observed=observed,
                    duration_ms=duration_ms,
                    note=f"guardrails could not run: {infra.get('detail') or infra.get('rule')}",
                )

        checks = evaluate(scenario.expect, observed)
        critical_failures = [c for c in checks if c.failed and c.critical]
        verdict: Verdict = "passed" if not critical_failures else "failed"
        log.info(
            "scenario_finished",
            scenario=scenario.id,
            verdict=verdict,
            checks=len(checks),
            failures=len(critical_failures),
        )
        return ScenarioResult(
            scenario=scenario,
            verdict=verdict,
            checks=checks,
            observed=observed,
            duration_ms=duration_ms,
        )

    async def _submit(self, scenario: Scenario) -> str:
        async with session_scope() as session:
            operator = (
                await session.execute(select(User).where(User.email == "operator@finops.local"))
            ).scalar_one_or_none()
            execution = await engine.submit(
                session,
                agent_key=scenario.agent_key,
                payload=dict(scenario.payload),
                user=operator,
                trigger="validation",
            )
            return execution.id

    async def _drive_to_terminal(self, execution_id: str, scenario: Scenario) -> None:
        """Wait for completion, deciding any approval the run raises along the way."""
        deadline = time.monotonic() + self.execution_timeout
        decided: set[str] = set()

        while time.monotonic() < deadline:
            async with session_scope() as session:
                execution = (
                    await session.execute(select(Execution).where(Execution.id == execution_id))
                ).scalar_one()
                status = execution.status

            if status in TERMINAL:
                return

            if status == "awaiting_approval":
                approval_id = await self._decide_pending_approval(execution_id, scenario, decided)
                if approval_id:
                    decided.add(approval_id)
                    continue

            await asyncio.sleep(self.poll_interval)

        raise TimeoutError(execution_id)

    async def _decide_pending_approval(
        self, execution_id: str, scenario: Scenario, already_decided: set[str]
    ) -> str | None:
        async with session_scope() as session:
            approval = (
                (
                    await session.execute(
                        select(Approval)
                        .where(Approval.execution_id == execution_id, Approval.status == "pending")
                        .order_by(Approval.created_at)
                    )
                )
                .scalars()
                .first()
            )
            if approval is None or approval.id in already_decided:
                return None

            reviewer = (
                await session.execute(select(User).where(User.email == self.reviewer_email))
            ).scalar_one_or_none()
            approved = scenario.approval_decision == "approve"
            now = datetime.now(UTC)

            approval.status = "approved" if approved else "rejected"
            approval.reviewer_id = reviewer.id if reviewer else None
            approval.reviewer_email = self.reviewer_email
            approval.comments = (
                f"Automated conformance run {scenario.id}: "
                f"{'approved' if approved else 'rejected'} per scenario policy"
            )
            approval.decided_at = now
            approval.timeline = [
                *(approval.timeline or []),
                {
                    "at": now.isoformat(),
                    "event": approval.status,
                    "by": self.reviewer_email,
                    "comments": approval.comments,
                },
            ]
            payload = dict(approval.payload or {})
            approval_id = approval.id
            await session.commit()

            log.info(
                "scenario_approval_decided",
                scenario=scenario.id,
                approval=approval_id,
                decision=approval.status,
            )
            await engine.resume_after_approval(
                session, execution_id, approved=approved, approval_payload=payload
            )
        return approval_id

    # ------------------------------------------------------------------ #
    # Observation                                                        #
    # ------------------------------------------------------------------ #
    async def _observe(self, execution_id: str) -> ObservedExecution:
        async with session_scope() as session:
            execution = (
                await session.execute(select(Execution).where(Execution.id == execution_id))
            ).scalar_one()
            spans = (
                (
                    await session.execute(
                        select(Span).where(Span.execution_id == execution_id).order_by(Span.start_time)
                    )
                )
                .scalars()
                .all()
            )
            approvals = (
                (await session.execute(select(Approval).where(Approval.execution_id == execution_id)))
                .scalars()
                .all()
            )

            output = execution.output or {}
            # A run the rails refused never produces an output, so its findings live only in
            # the event log. Reading them here is what lets a scenario assert *which* rail
            # fired instead of settling for "the run failed".
            guardrail_findings = list(output.get("guardrails") or [])
            if not guardrail_findings:
                events = (
                    (
                        await session.execute(
                            select(ExecutionEvent)
                            .where(
                                ExecutionEvent.execution_id == execution_id,
                                ExecutionEvent.type == "guardrail",
                            )
                            .order_by(ExecutionEvent.sequence)
                        )
                    )
                    .scalars()
                    .all()
                )
                guardrail_findings = [
                    finding for event in events for finding in (event.payload or {}).get("findings") or []
                ]

            tool_invocations = [
                (span.name.removeprefix("tool."), span.status == "ok")
                for span in spans
                if span.kind == "tool"
            ]

            return ObservedExecution(
                execution_id=execution.id,
                agent_key=execution.agent_key,
                status=execution.status,
                final_response=execution.final_response or "",
                output=output,
                error=execution.error,
                error_type=execution.error_type,
                cost_usd=execution.cost_usd or 0.0,
                latency_ms=execution.latency_ms,
                tokens=(execution.tokens_input or 0) + (execution.tokens_output or 0),
                llm_calls=execution.llm_call_count or 0,
                node_path=list(execution.node_path or []),
                tool_invocations=tool_invocations,
                citations=list(output.get("citations") or []),
                guardrail_findings=guardrail_findings,
                validation_findings=list(output.get("validation") or []),
                approvals=[
                    {
                        "id": a.id,
                        "status": a.status,
                        "risk_level": a.risk_level,
                        "node": a.node,
                        "title": a.title,
                        "payload": a.payload or {},
                        "reviewer_email": a.reviewer_email,
                    }
                    for a in approvals
                ],
                trace_id=execution.trace_id,
                span_count=len(spans),
            )


runner = ValidationRunner()
