"""Tests for the conformance suite and the execution validation agent.

The harness is what gates a release, so it is itself tested: the scenarios must be
well-formed, the assertion engine must catch real defects (not just report green), and
the runner must drive a genuine execution — including an approval gate — end to end.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.db.models.agents import Agent
from app.db.session import SessionFactory
from app.validation import (
    SCENARIOS,
    Expectation,
    ObservedExecution,
    ValidationRunner,
    agent_keys,
    by_id,
    evaluate,
    for_agent,
    to_console,
    to_json,
    to_markdown,
)
from tests.conftest import TEST_MODEL

IMPLEMENTED = {
    "customer_service",
    "kyc_onboarding",
    "aml_investigation",
    "investment_research",
    "knowledge_assistant",
}


class TestScenarioCatalogue:
    def test_twenty_scenarios_are_defined(self):
        assert len(SCENARIOS) == 20

    def test_ids_are_unique(self):
        ids = [s.id for s in SCENARIOS]
        assert len(set(ids)) == len(ids)

    def test_every_implemented_agent_is_covered_four_times(self):
        assert set(agent_keys()) == IMPLEMENTED
        for key in IMPLEMENTED:
            assert len(for_agent(key)) == 4, f"{key} should have four scenarios"

    def test_scenarios_target_implemented_agents_only(self):
        assert all(s.agent_key in IMPLEMENTED for s in SCENARIOS)

    def test_every_scenario_carries_an_input_and_a_rationale(self):
        for scenario in SCENARIOS:
            assert scenario.payload, f"{scenario.id} has no input"
            assert len(scenario.rationale) > 40, f"{scenario.id} has no meaningful rationale"

    def test_content_patterns_compile(self):
        for scenario in SCENARIOS:
            for pattern in scenario.expect.must_match + scenario.expect.must_not_match:
                re.compile(pattern)  # raises on a malformed expectation

    def test_suite_exercises_the_governance_paths(self):
        assert any(s.expect.approval_expected for s in SCENARIOS), "no approval gate covered"
        assert any(s.approval_decision == "reject" for s in SCENARIOS), "no rejection covered"
        assert any(s.expect.requires_citations for s in SCENARIOS), "no citation check"
        assert any(s.expect.must_not_match for s in SCENARIOS), "no negative assertion"
        assert any(s.expect.guardrail_rules_expected for s in SCENARIOS), "no guardrail check"
        assert any(s.expect.tools_forbidden for s in SCENARIOS), "no forbidden-tool check"

    def test_lookup_by_id(self):
        assert by_id("KA-01").agent_key == "knowledge_assistant"
        with pytest.raises(KeyError):
            by_id("NOPE-99")


def _observed(**overrides) -> ObservedExecution:
    base = {
        "execution_id": "exec-1",
        "agent_key": "knowledge_assistant",
        "status": "succeeded",
        "final_response": "A Sev-1 may be declared by the incident commander [1].",
        "output": {"response": "…", "usage": {}},
        "cost_usd": 0.01,
        "latency_ms": 2000,
        "tool_invocations": [("search_knowledge_base", True)],
        "citations": [{"id": "[1]", "title": "Incident Management Runbook"}],
        "span_count": 7,
        "trace_id": "trace-1",
    }
    base.update(overrides)
    return ObservedExecution(**base)


class TestAssertionEngine:
    """The assertions must fail when behaviour is wrong, not merely pass when it is right."""

    def test_clean_execution_passes(self):
        checks = evaluate(
            Expectation(
                tools_called=("search_knowledge_base",),
                must_match=(r"sev-?1",),
                requires_citations=True,
                output_keys=("response",),
            ),
            _observed(),
        )
        assert [c.name for c in checks if c.failed] == []

    def test_wrong_status_fails(self):
        checks = evaluate(Expectation(), _observed(status="failed", error="boom"))
        assert any(c.name == "status" and c.failed for c in checks)

    def test_missing_tool_fails(self):
        checks = evaluate(
            Expectation(tools_called=("analyse_portfolio",)), _observed()
        )
        failure = next(c for c in checks if c.name == "tools_called")
        assert failure.failed and "analyse_portfolio" in failure.detail

    def test_forbidden_tool_fails(self):
        checks = evaluate(
            Expectation(tools_forbidden=("search_knowledge_base",)), _observed()
        )
        assert any(c.name == "tools_forbidden" and c.failed for c in checks)

    def test_failed_tool_call_is_reported(self):
        checks = evaluate(
            Expectation(), _observed(tool_invocations=[("search_knowledge_base", False)])
        )
        assert any(c.name == "tool_success" and c.failed for c in checks)

    def test_missing_required_content_fails(self):
        checks = evaluate(Expectation(must_match=(r"quarterly earnings",)), _observed())
        assert any(c.name == "must_match" and c.failed for c in checks)

    def test_forbidden_content_fails(self):
        checks = evaluate(
            Expectation(must_not_match=(r"\b\d{16}\b",)),
            _observed(final_response="Your card number is 4111111111111111 as requested."),
        )
        assert any(c.name == "must_not_match" and c.failed for c in checks)

    def test_uncited_answer_fails_when_citations_required(self):
        checks = evaluate(
            Expectation(requires_citations=True),
            _observed(final_response="A Sev-1 is declared by the incident commander."),
        )
        assert any(c.name == "citations_referenced" and c.failed for c in checks)

    def test_retrieved_nothing_fails_when_citations_required(self):
        checks = evaluate(Expectation(requires_citations=True), _observed(citations=[]))
        assert any(c.name == "citations_retrieved" and c.failed for c in checks)

    def test_missing_approval_fails(self):
        checks = evaluate(
            Expectation(approval_expected=True, approval_on_tool="escalate_to_human"),
            _observed(),
        )
        names = {c.name for c in checks if c.failed}
        assert "approval_raised" in names

    def test_unexpected_approval_fails(self):
        checks = evaluate(
            Expectation(),
            _observed(approvals=[{"id": "a1", "status": "approved", "payload": {}}]),
        )
        assert any(c.name == "approval_unexpected" and c.failed for c in checks)

    def test_approval_on_the_wrong_tool_fails(self):
        checks = evaluate(
            Expectation(approval_expected=True, approval_on_tool="generate_sar"),
            _observed(approvals=[{"id": "a1", "status": "approved",
                                  "payload": {"tool": "escalate_to_human"}}]),
        )
        assert any(c.name == "approval_on_tool" and c.failed for c in checks)

    def test_missing_guardrail_fails(self):
        checks = evaluate(Expectation(guardrail_rules_expected=("disclaimer",)), _observed())
        assert any(c.name == "guardrails_applied" and c.failed for c in checks)

    def test_budget_breach_fails(self):
        checks = evaluate(Expectation(max_cost_usd=0.001), _observed(cost_usd=0.5))
        assert any(c.name == "cost_within_cap" and c.failed for c in checks)

    def test_latency_breach_is_a_warning_not_a_failure(self):
        checks = evaluate(Expectation(max_latency_ms=10), _observed(latency_ms=5000))
        latency = next(c for c in checks if c.name == "latency_within_sla")
        assert latency.failed and latency.critical is False

    def test_missing_output_key_fails(self):
        checks = evaluate(Expectation(output_keys=("citations",)), _observed(output={}))
        assert any(c.name == "output_keys" and c.failed for c in checks)

    def test_missing_trace_fails(self):
        checks = evaluate(Expectation(), _observed(span_count=0))
        assert any(c.name == "trace_recorded" and c.failed for c in checks)

    def test_content_checks_skip_when_no_answer_was_produced(self):
        checks = evaluate(
            Expectation(status="failed", must_match=(r"anything",)),
            _observed(status="failed", final_response=""),
        )
        assert any(c.name == "response_content" and c.outcome == "skip" for c in checks)
        assert not any(c.name == "must_match" for c in checks)


class TestRunnerEndToEnd:
    """Drives real executions through the engine using the suite's scripted provider."""

    async def _point_agent_at_scripted_model(self, agent_key: str) -> None:
        from app.agents.registry import agent_registry

        async with SessionFactory() as session:
            agent = (
                await session.execute(select(Agent).where(Agent.key == agent_key))
            ).scalar_one()
            config = {**(agent.config or {}), "model": TEST_MODEL}
            agent.config = config
            await session.commit()
            agent_registry.apply_override(agent_key, config)

    async def test_preflight_reports_environment_state(self):
        report = await ValidationRunner().preflight(list(SCENARIOS))
        assert report["agents_missing"] == []
        assert set(report["agents_required"]) == IMPLEMENTED
        assert report["reviewer_present"] is True
        assert report["sample_banking_customers"] > 0

    async def test_knowledge_scenario_passes_against_a_correct_answer(
        self, _register_scripted_model
    ):
        provider = _register_scripted_model
        provider.queue_text(
            '{"objective":"Explain the severity model","steps":[],"required_tools":'
            '["search_knowledge_base"],"needs_knowledge_search":true,"risk_level":"low"}'
        )
        provider.queue_tool_call(
            "search_knowledge_base", {"query": "incident severity classification", "top_k": 4}
        )
        provider.queue_text(
            "Sev-1 covers complete loss of a customer-facing service, a confirmed data "
            "breach, or a regulatory reporting failure [1]. It may be declared by the "
            "on-call incident commander or any engineer who believes the criteria are "
            "met [1]. Sources: Incident Management Runbook."
        )
        await self._point_agent_at_scripted_model("knowledge_assistant")

        result = await ValidationRunner().run_scenario(by_id("KA-01"))

        assert result.verdict == "passed", [c.to_dict() for c in result.failed_checks]
        assert result.observed is not None
        assert "search_knowledge_base" in result.observed.tools_called
        assert result.observed.citations
        assert result.observed.span_count > 3

    async def test_hallucinated_answer_is_caught(self, _register_scripted_model):
        """The out-of-corpus scenario must fail when the agent invents a figure."""
        provider = _register_scripted_model
        provider.queue_text(
            '{"objective":"Report the margin","steps":[],"required_tools":'
            '["search_knowledge_base"],"needs_knowledge_search":true,"risk_level":"low"}'
        )
        provider.queue_tool_call("search_knowledge_base", {"query": "net interest margin Q3 2025"})
        provider.queue_text(
            "Our net interest margin was 3.8% in the third quarter of 2025, with retail "
            "contributing 4.1% and corporate 3.2%."
        )
        await self._point_agent_at_scripted_model("knowledge_assistant")

        result = await ValidationRunner().run_scenario(by_id("KA-04"))

        assert result.verdict == "failed"
        failed = {c.name for c in result.failed_checks}
        assert {"must_match", "must_not_match"} & failed

    async def test_approval_gate_is_driven_and_validated(self, _register_scripted_model):
        """CS-04 suspends on escalate_to_human; the validator reviews and resumes it."""
        provider = _register_scripted_model
        provider.queue_text(
            '{"objective":"Escalate the fraud report","steps":[],"required_tools":'
            '["escalate_to_human"],"needs_knowledge_search":false,"risk_level":"high"}'
        )
        provider.queue_tool_call(
            "escalate_to_human",
            {"reason": "Customer reports an unauthorised card transaction", "urgency": "high"},
        )
        provider.queue_text(
            "I have escalated this to our Tier 2 specialist team and raised a ticket. They "
            "will contact you within four hours."
        )
        await self._point_agent_at_scripted_model("customer_service")

        result = await ValidationRunner().run_scenario(by_id("CS-04"))

        assert result.verdict == "passed", [c.to_dict() for c in result.failed_checks]
        assert result.observed is not None
        assert result.observed.status == "succeeded"
        assert len(result.observed.approvals) == 1
        approval = result.observed.approvals[0]
        assert approval["status"] == "approved"
        assert approval["payload"]["tool"] == "escalate_to_human"
        assert approval["reviewer_email"] == "approver@finops.local"

    async def test_report_renders_and_aggregates(self, _register_scripted_model):
        provider = _register_scripted_model
        for _ in range(2):
            provider.queue_text(
                '{"objective":"Answer","steps":[],"required_tools":[],'
                '"needs_knowledge_search":true,"risk_level":"low"}'
            )
            provider.queue_text(
                "The average monthly balance requirement is INR 10,000 in metro branches [1]."
            )
        await self._point_agent_at_scripted_model("knowledge_assistant")

        report = await ValidationRunner().run([by_id("KA-03"), by_id("KA-02")])

        assert len(report.results) == 2
        payload = report.to_dict()
        assert payload["summary"]["total"] == 2
        assert set(payload["by_agent"]) == {"knowledge_assistant"}
        assert "Agent conformance report" in to_markdown(report)
        assert '"summary"' in to_json(report)
        assert "KA-03" in to_console(report)

    async def test_unknown_agent_is_reported_as_an_error_not_a_crash(self):
        import dataclasses

        broken = dataclasses.replace(by_id("KA-01"), agent_key="does_not_exist")
        result = await ValidationRunner().run_scenario(broken)
        assert result.verdict == "error"
        assert "not registered" in result.note
