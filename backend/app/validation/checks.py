"""Assertion engine.

Turns an ``Expectation`` plus the observed execution into a list of individual pass/fail
checks. Keeping each assertion separate means a failing scenario reports *which*
behaviour broke, not just that something did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.validation.scenarios import Expectation

Outcome = Literal["pass", "fail", "skip"]


@dataclass
class CheckResult:
    name: str
    outcome: Outcome
    detail: str
    critical: bool = True

    @property
    def failed(self) -> bool:
        return self.outcome == "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "critical": self.critical,
        }


@dataclass
class ObservedExecution:
    """Everything the validator learned about one run, normalised for assertions."""

    execution_id: str
    agent_key: str
    status: str
    final_response: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    tokens: int = 0
    llm_calls: int = 0
    node_path: list[str] = field(default_factory=list)
    #: (tool name, succeeded) in invocation order, read from the trace spans.
    tool_invocations: list[tuple[str, bool]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    guardrail_findings: list[dict[str, Any]] = field(default_factory=list)
    validation_findings: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    span_count: int = 0

    @property
    def tools_called(self) -> set[str]:
        return {name for name, _ in self.tool_invocations}

    @property
    def failed_tools(self) -> list[str]:
        return [name for name, ok in self.tool_invocations if not ok]


def _search(patterns: tuple[str, ...], text: str) -> tuple[list[str], list[str]]:
    """Returns (matched, unmatched) patterns, case-insensitive."""
    matched, unmatched = [], []
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            matched.append(pattern)
        else:
            unmatched.append(pattern)
    return matched, unmatched


def evaluate(expect: Expectation, observed: ObservedExecution) -> list[CheckResult]:
    checks: list[CheckResult] = []
    response = observed.final_response or ""

    # --- lifecycle ---------------------------------------------------------
    checks.append(
        CheckResult(
            "status",
            "pass" if observed.status == expect.status else "fail",
            f"expected '{expect.status}', observed '{observed.status}'"
            + (f" ({observed.error_type}: {observed.error})" if observed.error else ""),
        )
    )

    # Downstream assertions are meaningless if the run never produced an answer.
    produced_answer = observed.status in {"succeeded", "awaiting_approval"}

    # --- tools -------------------------------------------------------------
    if expect.tools_called:
        missing = sorted(set(expect.tools_called) - observed.tools_called)
        checks.append(
            CheckResult(
                "tools_called",
                "pass" if not missing else "fail",
                f"required {list(expect.tools_called)}; "
                f"invoked {sorted(observed.tools_called) or 'none'}"
                + (f"; missing {missing}" if missing else ""),
            )
        )

    if expect.tools_forbidden:
        used = sorted(set(expect.tools_forbidden) & observed.tools_called)
        checks.append(
            CheckResult(
                "tools_forbidden",
                "pass" if not used else "fail",
                "none of the forbidden tools ran" if not used else f"forbidden tools ran: {used}",
            )
        )

    if expect.all_tools_ok and observed.tool_invocations:
        failed = observed.failed_tools
        checks.append(
            CheckResult(
                "tool_success",
                "pass" if not failed else "fail",
                f"{len(observed.tool_invocations) - len(failed)}/"
                f"{len(observed.tool_invocations)} tool calls succeeded"
                + (f"; failed: {failed}" if failed else ""),
            )
        )

    # --- content -----------------------------------------------------------
    if not produced_answer:
        checks.append(
            CheckResult("response_content", "skip", "no response produced; content not assessed")
        )
    else:
        checks.append(
            CheckResult(
                "response_length",
                "pass" if len(response) >= expect.min_response_chars else "fail",
                f"{len(response)} characters (minimum {expect.min_response_chars})",
            )
        )
        if expect.must_match:
            _, unmatched = _search(expect.must_match, response)
            checks.append(
                CheckResult(
                    "must_match",
                    "pass" if not unmatched else "fail",
                    "all required patterns present"
                    if not unmatched
                    else f"missing patterns: {unmatched}",
                )
            )
        if expect.must_not_match:
            matched, _ = _search(expect.must_not_match, response)
            checks.append(
                CheckResult(
                    "must_not_match",
                    "pass" if not matched else "fail",
                    "no forbidden patterns present"
                    if not matched
                    else f"forbidden patterns present: {matched}",
                )
            )

    # --- retrieval and grounding -------------------------------------------
    if expect.requires_citations:
        markers = set(re.findall(r"\[(\d+)\]", response))
        enough_sources = len(observed.citations) >= expect.min_citations
        checks.append(
            CheckResult(
                "citations_retrieved",
                "pass" if enough_sources else "fail",
                f"{len(observed.citations)} sources retrieved "
                f"(minimum {expect.min_citations})",
            )
        )
        checks.append(
            CheckResult(
                "citations_referenced",
                "pass" if markers else "fail",
                f"{len(markers)} citation markers in the response"
                if markers
                else "response cites no sources",
            )
        )

    # --- governance --------------------------------------------------------
    if expect.approval_expected:
        raised = bool(observed.approvals)
        checks.append(
            CheckResult(
                "approval_raised",
                "pass" if raised else "fail",
                f"{len(observed.approvals)} approval request(s)"
                if raised
                else "no approval was requested",
            )
        )
        if expect.approval_on_tool:
            tools = {(a.get("payload") or {}).get("tool") for a in observed.approvals}
            checks.append(
                CheckResult(
                    "approval_on_tool",
                    "pass" if expect.approval_on_tool in tools else "fail",
                    f"approval expected on '{expect.approval_on_tool}'; "
                    f"observed on {sorted(t for t in tools if t) or 'none'}",
                )
            )
    elif observed.approvals:
        checks.append(
            CheckResult(
                "approval_unexpected",
                "fail",
                f"{len(observed.approvals)} approval(s) raised but none expected",
            )
        )

    if expect.guardrail_rules_expected:
        applied = {str(f.get("rule", "")) for f in observed.guardrail_findings}
        missing = [
            rule
            for rule in expect.guardrail_rules_expected
            if not any(rule in applied_rule for applied_rule in applied)
        ]
        checks.append(
            CheckResult(
                "guardrails_applied",
                "pass" if not missing else "fail",
                f"applied {sorted(applied) or 'none'}"
                + (f"; missing {missing}" if missing else ""),
            )
        )

    if expect.validation_checks_must_pass:
        by_name = {str(f.get("check")): str(f.get("status")) for f in observed.validation_findings}
        bad = [
            name
            for name in expect.validation_checks_must_pass
            if by_name.get(name, "missing") != "pass"
        ]
        checks.append(
            CheckResult(
                "engine_validation",
                "pass" if not bad else "fail",
                f"engine checks {by_name}" + (f"; not passing: {bad}" if bad else ""),
            )
        )

    # --- budget and service level ------------------------------------------
    checks.append(
        CheckResult(
            "cost_within_cap",
            "pass" if observed.cost_usd <= expect.max_cost_usd else "fail",
            f"${observed.cost_usd:.5f} against a ${expect.max_cost_usd:.2f} cap",
        )
    )
    if observed.latency_ms is not None:
        checks.append(
            CheckResult(
                "latency_within_sla",
                "pass" if observed.latency_ms <= expect.max_latency_ms else "fail",
                f"{observed.latency_ms} ms against a {expect.max_latency_ms} ms budget",
                critical=False,
            )
        )

    # --- structured output -------------------------------------------------
    if expect.output_keys:
        missing = [key for key in expect.output_keys if key not in (observed.output or {})]
        checks.append(
            CheckResult(
                "output_keys",
                "pass" if not missing else "fail",
                "all required output keys present" if not missing else f"missing keys: {missing}",
            )
        )

    # --- trace integrity (always asserted; the platform's own guarantee) ----
    checks.append(
        CheckResult(
            "trace_recorded",
            "pass" if observed.span_count > 0 else "fail",
            f"{observed.span_count} spans recorded under trace {observed.trace_id}",
        )
    )

    return checks
