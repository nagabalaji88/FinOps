"""Annex IV technical documentation, generated from the running platform.

Article 11 of the EU AI Act requires a provider of a high-risk system to keep technical
documentation covering the nine headings of Annex IV, and to keep it current. Written by
hand it is stale the day after it is signed: it describes the agents, tools and controls the
author could remember, not the ones deployed.

So this generates it from the platform instead. The agent registry, the tool registry and
its human-review predicate, the guardrail configurations on disk, the conformance suite and
the adverse-action vocabulary are all read live, which means a tool added without an
approval gate, or an agent registered without an owner, shows up here as a gap rather than
going unmentioned.

What it will not do is assert conformity. Sections that only a person can answer -- the risk
management decisions of Article 9, the declaration of conformity, the post-market monitoring
plan -- are emitted as explicit gaps naming who owes them. A document that quietly filled
those in would be worse than no document, because it would read as though someone had.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.compliance.adverse_action import ADVERSE_ACTION_REASONS
from app.compliance.fairness import MEASURABLE_ATTRIBUTES, UNMEASURABLE_ATTRIBUTES
from app.core.config import settings
from app.db.models.agents import Agent, Execution
from app.tools import registry
from app.tools.base import REVIEW_EXEMPT_WRITES

#: Annex III point 5(b): creditworthiness assessment of natural persons. Everything else
#: this platform runs is in scope of the Act but is not Annex III high risk, and saying which
#: is which is the first thing a notified body asks.
HIGH_RISK_AGENTS: dict[str, str] = {
    "credit_risk": "Annex III, point 5(b) — evaluates the creditworthiness of natural persons",
}

#: Harmonised standards under Article 40 were still being drafted by CEN-CENELEC JTC 21 when
#: this was written, so there is no harmonised standard to claim conformity with. Listing the
#: standards actually applied, and labelling them as such, is honest; citing a harmonised
#: standard that does not exist yet is not.
STANDARDS_APPLIED: list[tuple[str, str]] = [
    ("ISO/IEC 42001", "AI management system — used as the control framework for this platform"),
    ("ISO/IEC 23894", "AI risk management guidance — informs the risk register structure"),
    ("NIST AI RMF 1.0", "Govern/Map/Measure/Manage functions mapped onto the control set"),
    ("OWASP Top 10 for LLM Applications", "Threat model for the input and output rails"),
]

#: Headings Annex IV asks for that no amount of code can answer. Each names the artefact and
#: the role that owes it, because an unassigned gap is the one that stays open.
HUMAN_OWED_SECTIONS: list[dict[str, str]] = [
    {
        "annex_iv": "2(g) · Article 9",
        "artefact": "Risk management system: the hazard register, residual-risk judgements "
        "and the acceptance decision for each high-risk agent",
        "owner": "Chief Risk Officer, with the model risk committee",
    },
    {
        "annex_iv": "4 · Article 15",
        "artefact": "Appropriateness of the accuracy metrics for the declared intended "
        "purpose, and the accuracy thresholds the system is held to in production",
        "owner": "Head of Credit Risk Modelling",
    },
    {
        "annex_iv": "8 · Article 47",
        "artefact": "Signed EU declaration of conformity, and the conformity assessment "
        "route taken under Article 43",
        "owner": "Compliance (as the provider's authorised representative)",
    },
    {
        "annex_iv": "9 · Article 72",
        "artefact": "Post-market monitoring plan: what is collected from live operation, "
        "who reviews it, on what cadence, and the serious-incident reporting route "
        "under Article 73",
        "owner": "Head of Operational Risk",
    },
]


def _rail_configs() -> dict[str, list[str]]:
    """The guardrail configuration shipped for each agent, read from disk."""
    root = Path(__file__).resolve().parent.parent / "guardrails" / "configs"
    out: dict[str, list[str]] = {}
    if not root.is_dir():
        return out
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        out[directory.name] = sorted(f.name for f in directory.iterdir() if f.is_file())
    return out


def _tool_inventory() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "category": tool.category,
            "writes_data": tool.writes_data,
            "requires_human_review": tool.requires_human_review,
            "approval_risk": tool.approval_risk if tool.requires_human_review else "",
            "permission": tool.required_permission or "",
            "idempotent": tool.idempotent,
        }
        for tool in registry.all()
    ]


async def collect(session: Any) -> dict[str, Any]:
    """Gather every fact the document is built from, so it can also be served as JSON."""
    from app.llm.router import router as model_router
    from app.validation.scenarios import SCENARIOS

    agents = (await session.execute(select(Agent).order_by(Agent.key))).scalars().all()
    executions = int((await session.execute(select(func.count(Execution.id)))).scalar_one())

    scenario_coverage: dict[str, int] = {}
    for scenario in SCENARIOS:
        scenario_coverage[scenario.agent_key] = scenario_coverage.get(scenario.agent_key, 0) + 1

    tools = _tool_inventory()
    rails = _rail_configs()

    agent_rows = [
        {
            "key": a.key,
            "name": a.name,
            "description": a.description,
            "category": a.category,
            "owner": a.owner,
            "owner_email": a.owner_email or "",
            "department": a.department,
            "version": a.version,
            "lifecycle_state": a.lifecycle_state,
            "availability": a.availability,
            "tools": list(a.tools or []),
            "knowledge_sources": list(a.knowledge_sources or []),
            "sla_latency_ms": a.sla_latency_ms,
            "monthly_budget_usd": a.monthly_budget_usd,
            "high_risk_basis": HIGH_RISK_AGENTS.get(a.key, ""),
            "scenarios": scenario_coverage.get(a.key, 0),
            "rails_configured": bool(rails.get(a.key)),
        }
        for a in agents
    ]

    gaps: list[dict[str, str]] = []
    if not [a for a in agent_rows if a["availability"] == "implemented"]:
        # Without this, a document generated against an unseeded database would state that
        # no agent is high risk -- true of the empty registry it read, and the opposite of
        # what a reader would take it to mean about the system.
        gaps.append(
            {
                "annex_iv": "1",
                "gap": "The agent registry is empty, so this document describes no system. "
                "It is not evidence about any deployment until it is generated against one.",
                "owner": "Platform Engineering",
            }
        )
    for row in agent_rows:
        if row["availability"] != "implemented":
            continue
        if not row["owner_email"]:
            gaps.append(
                {
                    "annex_iv": "1(a)",
                    "gap": f"Agent `{row['key']}` has no owner email on the registry entry, "
                    "so the accountable person is not identifiable from the system itself",
                    "owner": row["owner"],
                }
            )
        if not row["scenarios"]:
            gaps.append(
                {
                    "annex_iv": "2(h)",
                    "gap": f"Agent `{row['key']}` has no conformance scenario, so its "
                    "behaviour is not evidenced by the validation suite",
                    "owner": row["owner"],
                }
            )
        if not row["rails_configured"]:
            gaps.append(
                {
                    "annex_iv": "2(g)",
                    "gap": f"Agent `{row['key']}` ships no guardrail configuration",
                    "owner": row["owner"],
                }
            )
    # A write with no gate is only reachable via the published exemption list, so an
    # undocumented one means the list and the registry have drifted apart.
    undocumented = [
        t["name"]
        for t in tools
        if t["writes_data"] and not t["requires_human_review"] and t["name"] not in REVIEW_EXEMPT_WRITES
    ]
    for name in undocumented:
        gaps.append(
            {
                "annex_iv": "2(e)",
                "gap": f"Tool `{name}` writes data without a human-review gate and without a "
                "recorded exemption",
                "owner": "Platform Engineering",
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": {
            "system_name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "data_retention_days": settings.data_retention_days,
        },
        "agents": agent_rows,
        "tools": tools,
        "rails": rails,
        "scenario_count": len(SCENARIOS),
        "executions_on_record": executions,
        "providers_configured": model_router.configured_providers(),
        "adverse_action_reasons": dict(ADVERSE_ACTION_REASONS),
        "fairness_attributes": list(MEASURABLE_ATTRIBUTES),
        "fairness_attributes_unavailable": dict(UNMEASURABLE_ATTRIBUTES),
        "standards_applied": [{"standard": s, "use": u} for s, u in STANDARDS_APPLIED],
        "human_owed_sections": HUMAN_OWED_SECTIONS,
        "gaps": gaps,
    }


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    out.append("")
    return out


def to_markdown(facts: dict[str, Any]) -> str:
    provider = facts["provider"]
    agents = facts["agents"]
    tools = facts["tools"]
    implemented = [a for a in agents if a["availability"] == "implemented"]
    high_risk = [a for a in agents if a["high_risk_basis"]]
    gated = [t for t in tools if t["requires_human_review"]]

    lines = [
        f"# Annex IV technical documentation — {provider['system_name']}",
        "",
        f"Version `{provider['version']}` · generated {facts['generated_at']} from the running "
        f"platform (`{provider['environment']}`).",
        "",
        "> This document is generated from the agent registry, the tool registry, the "
        "guardrail configurations and the conformance suite. It describes what is deployed, "
        "not what was intended. It is **not** a declaration of conformity: the sections only "
        "a person can answer are listed in [Outstanding sections](#outstanding-sections) with "
        "the role that owes each one.",
        "",
        "## 1. General description of the AI system",
        "",
        f"`{provider['system_name']}` is an agent platform operated inside a financial "
        "institution. Each agent is a distinct AI system with its own intended purpose, tool "
        "permissions, knowledge sources and guardrail configuration; they share one execution "
        "engine, one model router and one audit trail.",
        "",
        f"- Agents registered: **{len(agents)}** ({len(implemented)} implemented)",
        f"- Annex III high-risk agents: **{len(high_risk)}**",
        f"- Tools available: **{len(tools)}**, of which **{len(gated)}** require a human "
        "decision before they run",
        f"- Conformance scenarios: **{facts['scenario_count']}**",
        f"- Executions on record in this deployment: **{facts['executions_on_record']}**",
        f"- Model providers configured: "
        f"{', '.join(f'`{p}`' for p in facts['providers_configured']) or '_none_'}",
        "",
        "### 1(a) Intended purpose and accountable owner",
        "",
    ]
    lines += _table(
        ["Agent", "Intended purpose", "Owner", "Department", "Version", "State"],
        [
            [
                f"`{a['key']}`",
                a["description"] or a["name"],
                f"{a['owner']} ({a['owner_email']})" if a["owner_email"] else a["owner"],
                a["department"],
                a["version"],
                a["lifecycle_state"],
            ]
            for a in implemented
        ],
    )

    lines += ["### 1(b) Classification under Annex III", ""]
    if high_risk:
        lines += _table(["Agent", "Basis"], [[f"`{a['key']}`", a["high_risk_basis"]] for a in high_risk])
        lines += [
            "Every other registered agent is in scope of the Act but is not an Annex III "
            "high-risk use. The obligations evidenced below are applied to the whole platform "
            "regardless, because a control that only runs for one agent is a control that "
            "breaks the first time an agent is reclassified.",
            "",
        ]
    elif implemented:
        lines += ["No registered agent is classified as Annex III high risk.", ""]
    else:
        lines += [
            "**The agent registry is empty.** This document therefore says nothing about "
            "which agents are high risk — not that none are. Generate it against a seeded "
            "deployment before treating it as evidence.",
            "",
        ]

    lines += [
        "## 2. Development process and system elements",
        "",
        "### 2(a) Architecture",
        "",
        "Every execution traverses the same graph, and the graph is the control surface:",
        "",
        "```",
        "input rails → planner → retriever → memory → LLM ⇄ tools",
        "            → validation → output rails → human approval → response",
        "```",
        "",
        "- **Input and output rails** run before the model sees a request and before a "
        "response leaves, fail closed, and are configured per agent.",
        "- **The planner** declares the tools a run intends to use and the risk it carries. "
        "An unparseable plan is recorded as a failed validation finding rather than silently "
        "defaulted, so a degraded run is visible in its own audit record.",
        "- **Tools** are the only way an agent reaches a system of record. Each one declares "
        "whether it writes, what permission it needs, and whether a human must decide first.",
        "- **Human approval** suspends the execution as a durable checkpoint. The run cannot "
        "continue until a reviewer who is not the requester decides.",
        "",
        "### 2(e) Human oversight measures (Article 14)",
        "",
        f"**{len(gated)}** of **{len(tools)}** tools cannot execute without a human decision. "
        "The predicate is a single property consulted by both the execution engine and the "
        "direct-invocation endpoint, so a write cannot be reached unreviewed through one path "
        "because the other was the one that checked.",
        "",
    ]
    lines += _table(
        ["Tool", "Category", "Writes", "Human review", "Risk", "Permission"],
        [
            [
                f"`{t['name']}`",
                t["category"],
                "yes" if t["writes_data"] else "no",
                "**required**" if t["requires_human_review"] else "not required",
                t["approval_risk"] or "—",
                f"`{t['permission']}`" if t["permission"] else "—",
            ]
            for t in tools
        ],
    )

    exemptions = [t for t in tools if t["writes_data"] and not t["requires_human_review"]]
    if exemptions:
        lines += [
            "#### Recorded exemptions",
            "",
            "These tools write data without a review gate. Each exemption is recorded in the "
            "codebase with the reason it is safe to leave ungated — in every case the write is "
            "evidence or a log entry, and the consequential action it informs is gated "
            "separately. The exemption, not the write, is the claim a reviewer should test.",
            "",
        ]
        lines += _table(
            ["Tool", "Why no gate"],
            [
                [f"`{t['name']}`", REVIEW_EXEMPT_WRITES.get(t["name"], "**no reason recorded**")]
                for t in exemptions
            ],
        )

    lines += [
        "### 2(g) Guardrail configurations in force",
        "",
    ]
    lines += _table(
        ["Agent", "Configuration files"],
        [[f"`{key}`", ", ".join(f"`{f}`" for f in files)] for key, files in facts["rails"].items()],
    )

    lines += [
        "### 2(h) Validation and testing procedures",
        "",
        f"A deterministic conformance suite of **{facts['scenario_count']}** scenarios executes "
        "the agents through the real engine, acts as the human reviewer when a run suspends, "
        "and asserts the observed behaviour against the record it was given. It is not an LLM "
        "judge, so its verdict is reproducible and gates the release pipeline.",
        "",
    ]
    lines += _table(
        ["Agent", "Scenarios", "Rails configured"],
        [
            [f"`{a['key']}`", a["scenarios"] or "**none**", "yes" if a["rails_configured"] else "**no**"]
            for a in implemented
        ],
    )

    lines += [
        "## 3. Monitoring, functioning and control",
        "",
        "- Every execution writes a trace: spans per node and per tool call, an ordered event "
        "log, the plan, the token and cost ledger, and the approval timeline with the "
        "reviewer's identity.",
        f"- Records are retained for **{provider['data_retention_days']} days** under the "
        "retention policy, which runs as a scheduled job rather than on request.",
        "- Readiness and health endpoints report provider reachability, durable artifact "
        "storage and database state, so a deployment that cannot meet its obligations fails "
        "its probe instead of serving.",
        "",
        "### 3(c) Transparency to the affected person — adverse-action reasons",
        "",
        f"A credit decline cannot be recorded without at least one reason code, and the code "
        f"must come from a published vocabulary of **{len(facts['adverse_action_reasons'])}** "
        "entries. An unrecognised code is rejected at the tool boundary. Each code carries the "
        "applicant-facing wording, so two declines for the same cause produce the same "
        "explanation and can be counted.",
        "",
    ]
    lines += _table(
        ["Code", "Reason given to the applicant"],
        [[f"`{c}`", r] for c, r in sorted(facts["adverse_action_reasons"].items())],
    )

    lines += [
        "## 4. Accuracy, robustness and fairness metrics (Article 15)",
        "",
        "Selection rates are measured across the protected attributes the platform holds, and "
        "the adverse impact ratio against the most favoured group is reported on every "
        "conformance run. The four-fifths convention flags a ratio below 0.80 for "
        "investigation; it is a screening threshold, not a verdict.",
        "",
        f"- Measured: {', '.join(f'`{a}`' for a in facts['fairness_attributes'])}",
        "",
    ]
    lines += _table(
        ["Attribute", "Why it is not measured"],
        [[f"`{k}`", v] for k, v in sorted(facts["fairness_attributes_unavailable"].items())],
    )
    lines += [
        "Those attributes are not collected and are deliberately not inferred: deriving a "
        "special category from a name or a postcode to produce a fairness number would create "
        "an inference the applicant never consented to. The gap is recorded here rather than "
        "closed with a guess.",
        "",
        "## 7. Standards applied",
        "",
        "No harmonised standard under Article 40 covers this system yet. The frameworks "
        "actually used as the control baseline are listed below, and are described as applied "
        "rather than as a presumption of conformity.",
        "",
    ]
    lines += _table(
        ["Framework", "How it is used"],
        [[s["standard"], s["use"]] for s in facts["standards_applied"]],
    )

    lines += [
        "## Outstanding sections",
        "",
        "Annex IV headings that this generator cannot answer, with the role that owes each. "
        "They are listed rather than omitted so that an incomplete file is self-evidently "
        "incomplete.",
        "",
    ]
    lines += _table(
        ["Annex IV", "Artefact owed", "Owner"],
        [[s["annex_iv"], s["artefact"], s["owner"]] for s in facts["human_owed_sections"]],
    )

    gaps = facts["gaps"]
    lines += ["## Gaps found in the deployed system", ""]
    if gaps:
        lines += [
            f"**{len(gaps)}** gap(s) were found by reading the platform. Each is a fact about "
            "what is deployed, not a style preference.",
            "",
        ]
        lines += _table(["Annex IV", "Gap", "Owner"], [[g["annex_iv"], g["gap"], g["owner"]] for g in gaps])
    else:
        lines += [
            "None. Every implemented agent has an identifiable owner, a guardrail "
            "configuration and conformance coverage, and every data-writing tool either "
            "requires human review or carries a recorded exemption.",
            "",
        ]
    return "\n".join(lines)


async def generate(session: Any) -> tuple[str, dict[str, Any]]:
    """The document and the facts it was built from."""
    facts = await collect(session)
    return to_markdown(facts), facts
