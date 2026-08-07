"""NeMo Guardrails integration.

Rail configurations live in ``app/guardrails/configs/<agent_key>/`` as ordinary NeMo
projects — ``config.yml``, Colang flows and prompts — so they can be edited, reviewed and
versioned like any other policy artefact.

Two rules govern the integration:

* **Rails never fail open.** If a rail cannot be evaluated the run is blocked, not waved
  through. The only exception is the absence of the rail configuration itself, which is
  reported as *not configured* rather than silently treated as a pass.
* **Deterministic first.** The pattern rails need no credentials, so they hold in every
  environment. The LLM-backed ``self check`` rails are removed from the configuration when
  no provider is configured, and the status says so, rather than leaving a rail that
  errors on every call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.guardrails.actions import DETECTORS, SEVERITY, TRANSFORMS, fail_closed

log = get_logger("guardrails.nemo")

CONFIG_ROOT = Path(__file__).parent / "configs"

#: Rail flows that require a model. Dropped when no provider is configured.
LLM_BACKED_FLOWS = {"self check input", "self check output", "self check facts"}


@dataclass
class RailFinding:
    """One rail decision, in the shape the engine records guardrail findings."""

    rule: str
    severity: str
    action: str  # blocked | masked | passed
    detail: str | None = None
    source: str = "nemo"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "action": self.action,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class RailResult:
    """Outcome of running one side of the rails."""

    evaluated: bool
    blocked: bool = False
    findings: list[RailFinding] = field(default_factory=list)
    text: str | None = None
    reason: str | None = None
    llm_rails: bool = False

    @property
    def modified(self) -> bool:
        return any(finding.action == "masked" for finding in self.findings)


def _parse_hit(raw: str) -> RailFinding:
    """Actions return ``rule|detail``; rail exceptions carry that string through."""
    rule, _, detail = raw.partition("|")
    rule = rule.strip() or "rail"
    return RailFinding(
        rule=rule,
        severity=SEVERITY.get(rule, "high"),
        action="blocked",
        detail=detail.strip() or None,
    )


class NemoGuardrails:
    """Loads and runs the per-agent rail configurations."""

    def __init__(self, root: Path | None = None):
        self._root = root or CONFIG_ROOT
        self._rails: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._import_error: str | None = None

    # --- availability --------------------------------------------------------
    def configured_agents(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            path.name for path in self._root.iterdir() if (path / "config.yml").is_file()
        )

    def covers(self, agent_key: str) -> bool:
        return settings.nemo_guardrails_enabled and (
            self._root / agent_key / "config.yml"
        ).is_file()

    def _llm_rails_available(self) -> bool:
        """LLM-backed rails need both a configured provider and the operator's consent."""
        from app.llm.router import router as model_router

        return settings.nemo_llm_rails_enabled and bool(model_router.configured_providers())

    # --- loading -------------------------------------------------------------
    async def _load(self, agent_key: str) -> Any | None:
        if agent_key in self._rails:
            return self._rails[agent_key]
        if agent_key in self._errors:
            return None

        async with self._lock:
            if agent_key in self._rails:
                return self._rails[agent_key]
            try:
                from nemoguardrails import LLMRails, RailsConfig
            except ImportError as exc:  # pragma: no cover - depends on the extra
                self._import_error = (
                    "nemoguardrails is not installed; install the 'guardrails' extra"
                )
                self._errors[agent_key] = self._import_error
                log.warning("nemo_guardrails_unavailable", error=str(exc))
                return None

            path = self._root / agent_key
            try:
                config = RailsConfig.from_path(str(path))
                has_llm = self._llm_rails_available()
                if not has_llm:
                    _strip_llm_flows(config)
                rails = LLMRails(config, llm=_router_llm(agent_key) if has_llm else None)
                # Detectors are wrapped here rather than at definition, so the
                # fail-closed guarantee holds for every registered rail.
                for name, action in DETECTORS.items():
                    rails.register_action(fail_closed(action), name)
                for name, action in TRANSFORMS.items():
                    rails.register_action(action, name)
            except Exception as exc:
                self._errors[agent_key] = str(exc)
                log.error("nemo_rails_load_failed", agent=agent_key, error=str(exc))
                return None

            self._rails[agent_key] = rails
            log.info("nemo_rails_loaded", agent=agent_key, llm_rails=has_llm)
            return rails

    # --- evaluation ----------------------------------------------------------
    async def check_input(self, agent_key: str, text: str,
                          *, context: dict[str, Any] | None = None) -> RailResult:
        return await self._run(agent_key, "input", text, context or {})

    async def check_output(self, agent_key: str, text: str, *, user_text: str = "",
                           context: dict[str, Any] | None = None) -> RailResult:
        return await self._run(agent_key, "output", text, context or {}, user_text=user_text)

    async def _run(self, agent_key: str, side: str, text: str,
                   context: dict[str, Any], *, user_text: str = "") -> RailResult:
        if not settings.nemo_guardrails_enabled:
            return RailResult(evaluated=False, reason="disabled")
        if not self.covers(agent_key):
            return RailResult(evaluated=False, reason="no_rail_config")

        rails = await self._load(agent_key)
        if rails is None:
            # The agent declares rails but they cannot be loaded. Running it unguarded
            # would silently drop a compliance control, so the run is refused instead.
            reason = self._errors.get(agent_key, "load_failed")
            return RailResult(
                evaluated=True, blocked=True, reason=reason,
                findings=[RailFinding("rails_unavailable", "critical", "blocked", reason)],
            )

        messages = (
            [{"role": "user", "content": text}]
            if side == "input"
            else [{"role": "user", "content": user_text or ""},
                  {"role": "assistant", "content": text}]
        )
        options = {
            "rails": [side],
            "output_vars": ["rail_masked"],
            "log": {"activated_rails": True},
        }
        try:
            response = await rails.generate_async(
                messages=messages, options=options,
                state={"context": {"pii_allowlist": context.get("pii_allowlist", [])}}
                if context.get("pii_allowlist") else None,
            )
        except Exception as exc:
            # A rail that cannot run is a blocked run, not a passed one.
            log.error("nemo_rail_error", agent=agent_key, side=side, error=str(exc))
            return RailResult(
                evaluated=True, blocked=True, reason=f"rail_error: {exc}",
                findings=[RailFinding("rail_error", "high", "blocked", str(exc)[:300])],
            )

        return _interpret(response, side=side, original=text,
                          llm_rails=self._llm_rails_available())

    # --- reporting -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            import nemoguardrails

            version = getattr(nemoguardrails, "__version__", "unknown")
            installed = True
        except ImportError:
            version, installed = None, False

        agents = self.configured_agents()
        llm_rails = self._llm_rails_available() if installed else False
        if not settings.nemo_guardrails_enabled:
            state = "disabled"
        elif not installed:
            state = "not_installed"
        elif not agents:
            state = "not_configured"
        elif self._errors:
            state = "degraded"
        else:
            state = "configured"
        return {
            "name": "nemo_guardrails",
            "category": "guardrails",
            "status": state,
            "enabled": settings.nemo_guardrails_enabled,
            "installed": installed,
            "version": version,
            "agents": agents,
            "loaded": sorted(self._rails),
            "llm_backed_rails": llm_rails,
            "note": None if llm_rails else
            "LLM-backed rails are off — deterministic rails only",
            "errors": dict(self._errors) or None,
            "required": ["nemoguardrails"] if not installed else [],
        }


def _router_llm(agent_key: str) -> Any:
    from app.guardrails.llm_adapter import RouterLLM

    return RouterLLM(context={"agent_key": agent_key, "component": "guardrails"})


def _strip_llm_flows(config: Any) -> None:
    """Remove rails that need a model, so the deterministic ones still run."""
    rails = getattr(config, "rails", None)
    if rails is None:
        return
    for side in ("input", "output"):
        section = getattr(rails, side, None)
        flows = getattr(section, "flows", None)
        if flows:
            section.flows = [flow for flow in flows if flow not in LLM_BACKED_FLOWS]


def _interpret(response: Any, *, side: str, original: str, llm_rails: bool) -> RailResult:
    """Turn a NeMo generation response into findings the engine can record."""
    payload = getattr(response, "response", response)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    content = payload.get("content") if isinstance(payload, dict) else str(payload)

    # With enable_rails_exceptions, a blocked rail returns a role="exception" message.
    role = payload.get("role") if isinstance(payload, dict) else None
    if role == "exception":
        detail = payload.get("content") or {}
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        finding = _parse_hit(str(message or "rail"))
        return RailResult(evaluated=True, blocked=True, findings=[finding],
                          reason=finding.rule, llm_rails=llm_rails)

    findings: list[RailFinding] = []
    output_data = getattr(response, "output_data", None) or {}
    masked = output_data.get("rail_masked")
    if masked:
        finding = _parse_hit(str(masked))
        if finding.rule == "rail_error":
            # The detector could not run. Blocking is the only safe reading.
            return RailResult(evaluated=True, blocked=True, findings=[finding],
                              reason=finding.detail, llm_rails=llm_rails)
        finding.action = "masked"
        findings.append(finding)

    text = content if isinstance(content, str) else None
    if side == "output" and text and text != original and not findings:
        findings.append(RailFinding("rail_rewrite", "low", "masked", "output rail rewrote the response"))

    return RailResult(evaluated=True, blocked=False, findings=findings,
                      text=text if side == "output" else None, llm_rails=llm_rails)


#: Process-wide instance; rail configs are immutable at runtime.
nemo_guardrails = NemoGuardrails()
