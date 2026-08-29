"""Report rendering for conformance runs: Markdown for humans, JSON for pipelines."""

from __future__ import annotations

import json

from app.validation.runner import ScenarioResult, ValidationReport

VERDICT_MARK = {"passed": "PASS", "failed": "FAIL", "blocked": "BLOCKED", "error": "ERROR"}


def to_json(report: ValidationReport, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent, default=str)


def _scenario_section(result: ScenarioResult) -> list[str]:
    lines = [
        f"### {result.scenario.id} · {result.scenario.title} — **{VERDICT_MARK[result.verdict]}**",
        "",
        f"_{result.scenario.rationale}_",
        "",
        f"- Agent: `{result.scenario.agent_key}`",
        f"- Input: `{json.dumps(result.scenario.payload, default=str)}`",
    ]
    if result.observed:
        observed = result.observed
        lines += [
            f"- Execution: `{observed.execution_id}` · trace `{observed.trace_id}`",
            f"- Status: `{observed.status}` · {observed.span_count} spans · "
            f"{observed.llm_calls} LLM calls · {len(observed.tool_invocations)} tool calls",
            f"- Cost: ${observed.cost_usd:.5f} · Latency: {observed.latency_ms} ms · "
            f"Tokens: {observed.tokens}",
        ]
        if observed.tool_invocations:
            tools = ", ".join(
                f"`{name}`{'' if ok else ' (failed)'}" for name, ok in observed.tool_invocations
            )
            lines.append(f"- Tools: {tools}")
        if observed.approvals:
            approvals = ", ".join(
                f"`{(a.get('payload') or {}).get('tool', a.get('node'))}` → {a['status']}"
                for a in observed.approvals
            )
            lines.append(f"- Approvals: {approvals}")
    if result.note:
        lines.append(f"- Note: {result.note}")

    if result.checks:
        lines += ["", "| Check | Result | Detail |", "|---|---|---|"]
        for check in result.checks:
            mark = {"pass": "pass", "fail": "**FAIL**", "skip": "skip"}[check.outcome]
            if check.failed and not check.critical:
                mark = "warn"
            detail = check.detail.replace("|", "\\|")
            lines.append(f"| `{check.name}` | {mark} | {detail} |")

    if result.observed and result.observed.final_response:
        excerpt = result.observed.final_response.strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600].rstrip() + " …"
        lines += ["", "<details><summary>Response</summary>", "", "```text", excerpt, "```", "", "</details>"]
    lines.append("")
    return lines


def to_markdown(report: ValidationReport) -> str:
    summary = report.to_dict()["summary"]
    lines = [
        "# Agent conformance report",
        "",
        f"Run {report.started_at.isoformat()} → {report.finished_at.isoformat()} "
        f"({(report.finished_at - report.started_at).total_seconds():.1f}s)",
        "",
        "## Summary",
        "",
        "| Scenarios | Passed | Failed | Blocked | Errored | Spend |",
        "|---|---|---|---|---|---|",
        f"| {summary['total']} | {summary['passed']} | {summary['failed']} | "
        f"{summary['blocked']} | {summary['errored']} | ${summary['total_cost_usd']:.5f} |",
        "",
    ]

    if report.preflight.get("warnings"):
        lines += ["### Preflight warnings", ""]
        lines += [f"- {warning}" for warning in report.preflight["warnings"]]
        lines.append("")

    lines += [
        "### By agent",
        "",
        "| Agent | Scenarios | Passed | Failed | Blocked | Errored |",
        "|---|---|---|---|---|---|",
    ]
    for agent_key, counts in report.to_dict()["by_agent"].items():
        lines.append(
            f"| `{agent_key}` | {counts['total']} | {counts['passed']} | {counts['failed']} | "
            f"{counts['blocked']} | {counts['errored']} |"
        )
    lines.append("")

    lines += [
        "### Scenario index",
        "",
        "| ID | Agent | Scenario | Verdict | Checks |",
        "|---|---|---|---|---|",
    ]
    for result in report.results:
        passed = sum(1 for c in result.checks if c.outcome == "pass")
        lines.append(
            f"| `{result.scenario.id}` | `{result.scenario.agent_key}` | {result.scenario.title} | "
            f"**{VERDICT_MARK[result.verdict]}** | {passed}/{len(result.checks)} |"
        )
    lines.append("")

    failures = [r for r in report.results if r.verdict in {"failed", "error"}]
    if failures:
        lines += ["## Failures", ""]
        for result in failures:
            lines += _scenario_section(result)

    lines += ["## All scenarios", ""]
    for result in report.results:
        lines += _scenario_section(result)

    return "\n".join(lines)


LABEL_WIDTH = 10


def _field(label: str, text: str, width: int = 96) -> list[str]:
    """A labelled, wrapped block: the label on the first line, continuation aligned."""
    import textwrap

    indent = " " * LABEL_WIDTH
    body: list[str] = []
    for paragraph in str(text).strip().splitlines():
        if paragraph.strip():
            body.extend(textwrap.wrap(paragraph.strip(), width=width - LABEL_WIDTH) or [""])
    if not body:
        return []
    return [f"  {label:<{LABEL_WIDTH - 2}}{body[0]}"] + [indent + line for line in body[1:]]


def format_live(
    index: int,
    total: int,
    result: ScenarioResult,
    *,
    show_output: bool = True,
    max_output_lines: int = 12,
    width: int = 96,
) -> str:
    """Readable block printed as each scenario finishes, so a run is watchable."""
    scenario = result.scenario
    observed = result.observed
    band = f"[{scenario.band}] " if scenario.band else ""
    head = f"[{index:>2}/{total}] {scenario.id:<10} {scenario.agent_key:<21} {band}{scenario.title}"
    lines = ["", head, "-" * min(len(head), width)]

    payload = json.dumps(scenario.payload, default=str)
    lines += _field("input", payload if len(payload) <= 400 else payload[:397] + "...", width)
    if scenario.data_basis:
        # The record, not the wording, is what a band scenario is asserting about. Printing it
        # next to the result means a failure can be read without opening the seed.
        lines += _field("data", scenario.data_basis, width)

    facts = [VERDICT_MARK[result.verdict], f"{result.duration_ms / 1000:.1f}s"]
    if observed:
        facts += [
            f"${observed.cost_usd:.5f}",
            f"{len(observed.tool_invocations)} tools",
            f"{observed.span_count} spans",
        ]
    if result.checks:
        passed = sum(1 for c in result.checks if c.outcome == "pass")
        facts.append(f"{passed}/{len(result.checks)} checks")
    lines += _field("result", " \u00b7 ".join(facts), width)

    if observed and observed.tool_invocations:
        lines += _field(
            "tools",
            ", ".join(f"{n}{'' if ok else ' (failed)'}" for n, ok in observed.tool_invocations),
            width,
        )

    if observed:
        for approval in observed.approvals:
            tool = (approval.get("payload") or {}).get("tool") or approval.get("node")
            lines += _field(
                "approval",
                f"{tool} -> {approval['status']} by {approval.get('reviewer_email') or 'unknown'}",
                width,
            )

    if result.verdict == "failed":
        for check in result.failed_checks:
            lines += _field("FAILED", f"{check.name}: {check.detail}", width)
    elif result.verdict in {"blocked", "error"} and result.note:
        lines += _field("reason", result.note, width)

    if show_output and observed and observed.final_response:
        body = _field("output", observed.final_response, width)
        lines += body[:max_output_lines]
        if len(body) > max_output_lines:
            lines.append(" " * LABEL_WIDTH + f"... ({len(observed.final_response)} characters in total)")
    return "\n".join(lines)


def to_console(report: ValidationReport) -> str:
    """Compact terminal summary."""
    lines = []
    for result in report.results:
        mark = VERDICT_MARK[result.verdict]
        detail = ""
        if result.verdict == "failed":
            detail = "; ".join(f"{c.name}: {c.detail}" for c in result.failed_checks[:3])
        elif result.verdict in {"blocked", "error"}:
            detail = result.note
        lines.append(
            f"  [{mark:7}] {result.scenario.id:7} {result.scenario.agent_key:22} "
            f"{result.scenario.title[:44]:44} {result.duration_ms:>7} ms"
            + (f"\n            → {detail}" if detail else "")
        )
    summary = report.to_dict()["summary"]
    lines += [
        "",
        f"  {summary['passed']} passed · {summary['failed']} failed · "
        f"{summary['blocked']} blocked · {summary['errored']} errored "
        f"· ${summary['total_cost_usd']:.5f} spent",
    ]
    return "\n".join(lines)
