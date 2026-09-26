"""Conformance suite: scenario inputs, assertions and the execution validation agent."""

from app.validation.checks import CheckResult, ObservedExecution, evaluate
from app.validation.report import to_console, to_json, to_markdown
from app.validation.runner import ScenarioResult, ValidationReport, ValidationRunner, runner
from app.validation.scenarios import SCENARIOS, Expectation, Scenario, agent_keys, by_id, for_agent

__all__ = [
    "CheckResult",
    "ObservedExecution",
    "evaluate",
    "to_console",
    "to_json",
    "to_markdown",
    "ScenarioResult",
    "ValidationReport",
    "ValidationRunner",
    "runner",
    "SCENARIOS",
    "Expectation",
    "Scenario",
    "agent_keys",
    "by_id",
    "for_agent",
]
