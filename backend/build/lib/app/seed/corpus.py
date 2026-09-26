"""Baseline enterprise knowledge corpus and source definitions.

This is the sample enterprise configuration for a banking deployment: the policies,
runbooks and architecture notes an institution would load on day one. Real deployments
replace or extend it by syncing the Confluence/SharePoint/Jira connectors.
"""

from __future__ import annotations

from typing import Any

KNOWLEDGE_SOURCES: list[dict[str, Any]] = [
    {
        "key": "banking_policies",
        "name": "Retail Banking Policies",
        "connector": "upload",
        "description": "Product terms, servicing policies and customer-facing procedures.",
        "status": "connected",
        "classification": "internal",
    },
    {
        "key": "compliance_policies",
        "name": "Compliance & Financial Crime Policies",
        "connector": "upload",
        "description": "KYC, AML, sanctions and regulatory reporting policy set.",
        "status": "connected",
        "classification": "confidential",
    },
    {
        "key": "aml_typologies",
        "name": "AML Typology Library",
        "connector": "upload",
        "description": "FATF and internal typology descriptions used by investigators.",
        "status": "connected",
        "classification": "confidential",
    },
    {
        "key": "engineering_docs",
        "name": "Engineering Architecture Docs",
        "connector": "confluence",
        "description": "Platform architecture, service contracts and design decisions.",
        "status": "not_configured",
        "classification": "internal",
        "config": {"space_key": "ENG", "limit": 100},
    },
    {
        "key": "runbooks",
        "name": "Operational Runbooks",
        "connector": "upload",
        "description": "Incident response, on-call and disaster-recovery runbooks.",
        "status": "connected",
        "classification": "internal",
    },
    {
        "key": "meeting_notes",
        "name": "Meeting Notes",
        "connector": "sharepoint",
        "description": "Governance forum and steering committee minutes.",
        "status": "not_configured",
        "classification": "confidential",
    },
    {
        "key": "product_catalogue",
        "name": "Product Catalogue",
        "connector": "upload",
        "description": "Deposit, lending, card and wealth product specifications.",
        "status": "connected",
        "classification": "internal",
    },
    {
        "key": "research_library",
        "name": "Investment Research Library",
        "connector": "upload",
        "description": "House view, methodology notes and published research.",
        "status": "connected",
        "classification": "internal",
    },
    {
        "key": "jira_delivery",
        "name": "Jira Delivery Board",
        "connector": "jira",
        "description": "Delivery backlog and incident tickets.",
        "status": "not_configured",
        "classification": "internal",
        "config": {"jql": "project = PLAT ORDER BY updated DESC", "limit": 100},
    },
    {
        "key": "slack_platform",
        "name": "Slack #platform-engineering",
        "connector": "slack",
        "description": "Engineering channel history.",
        "status": "not_configured",
        "classification": "internal",
        "config": {"channel_id": "", "channel_name": "platform-engineering"},
    },
]


SEED_DOCUMENTS: list[dict[str, Any]] = [
    {
        "source_key": "compliance_policies",
        "external_id": "POL-KYC-001",
        "title": "Customer Due Diligence Policy (CDD-001)",
        "author": "Financial Crime Compliance",
        "classification": "confidential",
        "content": """# Customer Due Diligence Policy (CDD-001)

## 1. Scope
This policy applies to all customer relationships originated through any channel, including
digital onboarding, branch onboarding and partner referrals.

## 2. Identification requirements
Every applicant must provide at least one Officially Valid Document (OVD) establishing identity
and one establishing current address. Acceptable identity documents are passport, Aadhaar,
PAN (identity only), voter ID and driving licence. Address may be evidenced by a utility bill
no older than 90 days, a bank statement, a registered rent agreement or an Aadhaar record.

## 3. Verification standards
- Passport data must be read from the machine readable zone and all four ICAO 9303 check digits
  must validate. A failed composite check digit is a hard failure.
- Aadhaar numbers must satisfy the Verhoeff checksum. Only the last four digits may be stored or
  displayed anywhere in the platform.
- PAN must match the ITD format AAAAA9999A. The fourth character encodes holder type and must be
  "P" for an individual applicant.
- Biometric liveness and face match are mandatory for fully digital onboarding. The match score
  threshold is 0.75. Scores between 0.60 and 0.75 require a manual review; below 0.60 is a
  rejection.

## 4. Risk classification
Customers are classified low, medium, high or critical using the enterprise risk model. Inputs
are sanctions screening results, PEP status, geography, occupation, product set, expected volume
relative to declared income and document verification outcomes.

## 5. Enhanced due diligence
EDD is mandatory for high and critical risk, all PEP relationships, all non-resident applicants
from FATF grey- or black-listed jurisdictions, and any relationship where source of funds cannot
be corroborated. EDD requires senior compliance sign-off before account activation.

## 6. Prohibited relationships
The bank will not onboard: any person or entity appearing on a sanctions list applicable to the
bank; shell banks; anonymous or numbered accounts; or any applicant who refuses to provide
beneficial ownership information.

## 7. Periodic review
Low risk: every 5 years. Medium: every 3 years. High and critical: annually, or immediately on a
material change in behaviour or profile.

## 8. Record keeping
All CDD records, screening evidence and decision rationale are retained for 5 years after the
relationship ends, in tamper-evident storage with full audit trail.
""",
    },
    {
        "source_key": "compliance_policies",
        "external_id": "POL-AML-002",
        "title": "AML Transaction Monitoring & SAR Policy (AML-002)",
        "author": "Money Laundering Reporting Officer",
        "classification": "confidential",
        "content": """# AML Transaction Monitoring & SAR Policy (AML-002)

## 1. Monitoring coverage
All customer transactions across all channels are subject to automated monitoring. Rules run
continuously; population-level scans run daily.

## 2. Rule set
| Code | Rule | Severity |
|------|------|----------|
| R001 | Structuring: two or more transactions between 90% and 100% of the reporting threshold on the same day | High |
| R002 | Rapid movement of funds: outflow of 80% or more of a large credit within 48 hours | High |
| R003 | High-risk jurisdiction exposure | High |
| R004 | Three or more round-value transfers of INR 100,000 or above | Medium |
| R005 | Volume spike of 4x or more against the trailing baseline | Medium |
| R006 | Dormant account (120+ days) reactivated with value of INR 500,000 or above | Medium |
| R007 | Single counterparty concentration above 70% of volume | Low |

The reporting threshold is INR 1,000,000 for cash and equivalent transactions.

## 3. Alert handling
Alerts must be triaged within 3 business days. Triage assigns one of: close as false positive
with rationale, escalate to case, or request further information. All decisions require a
documented rationale.

## 4. Investigation standard
Investigations must establish the expected profile, describe the observed activity, quantify the
deviation, identify counterparties and jurisdictions, and state why the activity is suspicious.
Investigators must distinguish observation from inference. Speculation without supporting data is
not acceptable evidence.

## 5. Suspicious Activity Reports
A SAR is filed where there are reasonable grounds to suspect proceeds of crime, terrorist
financing or a predicate offence. The narrative must cover who, what, when, where, why suspicious
and how funds moved, quoting exact amounts, dates, references and counterparties.

SARs must be filed with FIU-IND within 7 days of the suspicion being established. The MLRO or a
delegated deputy is the only approver. Draft SARs generated by automated tooling always require
human approval before filing.

## 6. Tipping off
Disclosing to a customer, or to any third party, that a SAR has been or may be filed is a
criminal offence. All AML work product is marked confidential and access is restricted to the
financial crime function.

## 7. Record keeping
Case files, evidence, narratives and filing confirmations are retained for 5 years from the date
of filing.
""",
    },
    {
        "source_key": "aml_typologies",
        "external_id": "TYP-001",
        "title": "Financial Crime Typology Library",
        "author": "Financial Crime Intelligence",
        "classification": "confidential",
        "content": """# Financial Crime Typology Library

## Structuring (smurfing)
Splitting a large value into multiple smaller transactions to stay below a reporting threshold.
Indicators: repeated amounts just under the threshold, multiple same-day deposits across branches
or channels, amounts that change immediately after a threshold change.

## Pass-through / funnel accounts
An account receives a large credit and disperses it almost entirely within a short window,
retaining little or no balance. Indicators: outflow ratio above 0.8 within 48 hours, unrelated
counterparties, no economic rationale for the flow.

## Trade-based money laundering
Misrepresenting price, quantity or quality of goods to move value across borders. Indicators:
invoices inconsistent with market prices, round-value payments to trading entities in high-risk
jurisdictions, goods descriptions that do not match the customer's stated business.

## Layering through round values
Repeated transfers of round amounts between related parties to obscure the audit trail.
Indicators: amounts that are exact multiples of 100,000, circular flows, short holding periods.

## Mule account networks
Third parties recruited to receive and forward funds. Indicators: new accounts with immediate
high-value throughput, shared devices or IP addresses across unrelated customers, young or
student customers with volumes inconsistent with declared income.

## Dormant account reactivation
A long-dormant account suddenly transacts at high value. Indicators: 120+ day inactivity followed
by significant credits, change of contact details shortly before reactivation.

## Sanctions evasion
Use of intermediaries, aliases or transhipment points to obscure a sanctioned ultimate party.
Indicators: counterparties in neighbouring jurisdictions to a sanctioned state, name variations
close to but not matching a listed party, unusual routing.
""",
    },
    {
        "source_key": "banking_policies",
        "external_id": "POL-SRV-010",
        "title": "Customer Servicing & Escalation Policy",
        "author": "Retail Banking Operations",
        "content": """# Customer Servicing & Escalation Policy

## Authentication before disclosure
No account, card, loan or transaction information may be disclosed until the customer has been
authenticated. Acceptable factors are the telephone banking PIN or the registered security
question. Three consecutive failures lock the profile and require branch identification.

## Data minimisation
Never disclose a full card number, full account number, PAN or Aadhaar number. Use the masked
values returned by servicing systems.

## Escalation triggers
Escalate to the Tier 2 specialist desk when the customer:
- alleges fraud or an unauthorised transaction;
- requests a formal complaint or mentions the Banking Ombudsman;
- has an unresolved issue older than 5 working days;
- is identified as vulnerable; or
- expresses severe dissatisfaction (sentiment score below -0.6).

## Service level agreements
| Priority | First response | Resolution |
|----------|----------------|------------|
| Urgent   | 1 hour         | 4 hours    |
| High     | 4 hours        | 8 hours    |
| Medium   | 8 hours        | 24 hours   |
| Low      | 24 hours       | 72 hours   |

## Unauthorised transactions
Under RBI rules, a customer reporting an unauthorised electronic transaction within 3 working
days has zero liability. Raise a dispute case immediately, issue provisional credit within
10 working days and inform the customer of the reference number in the same interaction.

## Complaint handling
All complaints are logged with a unique reference, acknowledged the same day and resolved within
30 days. Customers must be told of their right to escalate to the Banking Ombudsman if not
satisfied.
""",
    },
    {
        "source_key": "product_catalogue",
        "external_id": "PRD-001",
        "title": "Retail Product Catalogue",
        "author": "Product Management",
        "content": """# Retail Product Catalogue

## Savings accounts
- **Everyday Savings** - AMB INR 10,000 metro / 5,000 semi-urban / 2,500 rural. Interest 3.0% up
  to INR 5 lakh, 3.5% above. Free: 5 ATM transactions per month, RuPay debit card.
- **Salary Advantage** - zero AMB with a monthly salary credit. Interest as above. Free unlimited
  ATM access and a complimentary personal accident cover of INR 10 lakh.
- **Senior First** - for customers aged 60+. Additional 0.5% interest, doorstep banking, free
  demand drafts.

## Current accounts
- **Business Standard** - AMB INR 25,000. Free NEFT/RTGS. Cash deposit free up to INR 5 lakh
  per month.
- **Business Plus** - AMB INR 100,000. Overdraft up to INR 500,000 subject to assessment.

## Credit cards
- **Signature Rewards** - joining fee INR 2,500, waived on annual spend of INR 3 lakh. 4 reward
  points per INR 150 on dining and travel, 1 point otherwise. APR 36-42% annualised.
- **Platinum Travel** - joining fee INR 5,000. Lounge access 8 visits per year. 5% travel cashback
  capped at INR 5,000 per quarter.
- **Cashback Plus** - no joining fee. 2% cashback on online spend capped at INR 1,000 per month.

Late payment fee: INR 500 up to INR 10,000 outstanding; INR 750 up to INR 25,000; INR 1,200 above.

## Loans
- **Personal Loan** - INR 50,000 to 40 lakh, 10.5-18% p.a., tenure 12-72 months, processing fee
  up to 2%.
- **Home Loan** - up to INR 10 crore, 8.4-9.6% p.a., tenure up to 30 years, LTV up to 80%.
- **Auto Loan** - up to 100% on-road funding for approved models, 9.1-12% p.a., tenure up to
  84 months.
- **Education Loan** - up to INR 1.5 crore for approved institutions, moratorium of course
  duration plus 12 months.

## Wealth
- **Balanced Growth Mandate** - discretionary, moderate risk, benchmark NIFTY 50, minimum
  investment INR 25 lakh, management fee 0.9% p.a.
""",
    },
    {
        "source_key": "runbooks",
        "external_id": "RUN-001",
        "title": "Incident Management Runbook",
        "author": "Platform Engineering",
        "content": """# Incident Management Runbook

## Severity classification
- **Sev-1** - complete loss of a customer-facing service, confirmed data breach, or regulatory
  reporting failure. Declared by the on-call incident commander or any engineer who believes the
  criteria are met. Executive notification within 15 minutes.
- **Sev-2** - major degradation, a critical agent failing for all users, or sustained error rate
  above 25%. Notification within 30 minutes.
- **Sev-3** - partial degradation with a workaround. Handled in business hours.
- **Sev-4** - cosmetic or low-impact issue. Handled through the backlog.

Only the incident commander closes a Sev-1. Any engineer may declare one; nobody is penalised for
a false alarm.

## On-call rotation
Primary and secondary on-call rotate weekly. Acknowledgement SLA is 5 minutes for Sev-1/2. The
escalation path is primary -> secondary -> engineering manager -> CTO.

## Agent platform incidents
1. Check `/health/ready` and `/api/v1/services` for provider and infrastructure status.
2. Inspect circuit breaker state at `/api/v1/platform-metrics`. An open LLM breaker means the
   provider is failing; check the fallback chain has an available model.
3. Review recent failed executions and their traces. Node-level spans show which stage failed.
4. If a specific agent is failing, pause it (`POST /api/v1/agents/{key}/lifecycle`) rather than
   taking down the platform.
5. If cost anomalies are suspected, check the cost dashboard for a spike by model or agent, and
   apply the per-execution cap.

## Rollback
Agent configuration changes are versioned. Roll back with
`POST /api/v1/agents/{key}/versions/{version}/rollback`. Application rollback is a Helm rollback
to the previous release; database migrations are forward-only and must be backwards compatible
for one release.

## Post-incident
A blameless review is required for every Sev-1 and Sev-2 within 5 working days, with actions
tracked to completion.
""",
    },
    {
        "source_key": "runbooks",
        "external_id": "RUN-002",
        "title": "Backup, Restore and Disaster Recovery Runbook",
        "author": "Platform Engineering",
        "content": """# Backup, Restore and Disaster Recovery Runbook

## Objectives
- RPO: 15 minutes for the primary database, 1 hour for object storage.
- RTO: 1 hour for the API tier, 4 hours for full platform restoration.

## Backup schedule
- PostgreSQL: continuous WAL archiving to object storage plus a nightly base backup at 01:00 UTC.
  Retention 35 days.
- Object store (artifacts, reports, SARs): versioned buckets with cross-region replication.
  Retention 7 years for regulatory artifacts.
- Vector store: rebuildable from the primary database; a weekly snapshot is taken to shorten
  recovery.
- Configuration and secrets: Vault snapshots every 6 hours, encrypted, retained 90 days.

## Restore procedure
1. Provision the target cluster and restore the database from the most recent base backup.
2. Replay WAL to the desired point in time.
3. Restore object storage from the replicated bucket.
4. Rebuild the vector index: `finops reindex-knowledge`.
5. Verify with the smoke suite: authentication, an agent execution, a knowledge search and an
   approval decision.

## Testing
A full restore rehearsal is performed quarterly in an isolated environment and evidenced for
audit. Backup integrity checks run nightly and alert on failure.

## Data retention and erasure
Execution traces, logs and cost records are retained for the configured retention period
(default 400 days) and then purged by the retention job. Regulatory artifacts (KYC reports, SARs)
are exempt from routine purge and follow the 5-year statutory schedule.
""",
    },
    {
        "source_key": "engineering_docs",
        "external_id": "ARC-001",
        "title": "AI Agent Platform Architecture",
        "author": "Platform Architecture",
        "content": """# AI Agent Platform Architecture

## Overview
The platform runs agents as a deterministic graph over a shared execution engine. The graph is
planner -> retriever -> memory -> LLM <-> tools -> validation -> guardrails -> human approval ->
response. Each node opens a span, emits streaming events and can be retried independently.

## Components
- **API tier** (FastAPI) - authentication, RBAC, dashboards, streaming endpoints.
- **Execution engine** - runs the graph, enforces budgets and concurrency, checkpoints state for
  human-in-the-loop suspension and resume.
- **Model router** - dynamic model selection by capability, tier and cost, with per-provider
  circuit breakers and a fallback chain.
- **Tool registry** - schema-validated tools with timeouts, retries, health tracking and approval
  gating.
- **RAG pipeline** - chunking, embedding, hybrid vector and BM25 retrieval with citations.
- **Observability** - OpenTelemetry spans, Prometheus metrics, structured logs correlated by
  trace, correlation and request identifiers.

## State and durability
Execution state is checkpointed to the database whenever the graph suspends for approval. A
resume restores messages, plan, retrieved context, tool results and budget, then re-enters at the
suspended node. Events are appended to an event log so late subscribers can replay a stream from
any sequence number.

## Failure handling
Every external call is wrapped in retry with exponential backoff and jitter, and a circuit
breaker per provider or tool. A bulkhead caps concurrent executions. Budgets are enforced per
execution and reported per agent, user, department, model and tool.

## Security model
Zero trust: every endpoint requires authentication; RBAC is checked per operation; all mutations
are audit logged with actor, resource, outcome and trace identifiers. Secrets resolve from Vault
when configured, otherwise from environment or encrypted database rows. PII is masked by the
guardrail node before a response leaves the platform.

## Scaling
The API and engine are stateless and scale horizontally; live streaming fans out through Redis
pub/sub so any pod can serve any subscriber. Long-running and scheduled work is dispatched to
Celery workers. Kafka receives a durable copy of domain events when configured.
""",
    },
    {
        "source_key": "research_library",
        "external_id": "RES-001",
        "title": "Investment Research Methodology & House View",
        "author": "Investment Research Desk",
        "content": """# Investment Research Methodology & House View

## Research standard
Every note separates observation, analysis and recommendation. Observations cite the data source
and as-of date. Analysis states the method used. Recommendations carry a thesis, a valuation
anchor, catalysts, at least two downside risks and an explicit time horizon.

## Valuation approach
Primary method is a discounted cash flow with an explicit five-year forecast and a fade to
terminal growth. Cross-checks are relative multiples against a defined peer set (EV/EBITDA, P/E,
P/B for financials) and, where relevant, sum-of-the-parts.

## Risk framework
Portfolio risk is measured with historical simulation: 95% one-day VaR and expected shortfall
over a rolling 252-day window, plus annualised volatility and beta versus the mandate benchmark.
Concentration is assessed with the Herfindahl index; above 0.25 is treated as concentrated.

## Position sizing
No single equity position exceeds 20% of a discretionary mandate. Sector exposure is capped at
35%. Cash floor is 2%.

## Coverage and conflicts
Analysts must disclose personal holdings in covered names. Research is embargoed until published
through the approved channel. Draft notes produced with AI assistance require named analyst
review and sign-off before publication.

## Compliance
All client-facing research must carry the standard disclaimer: information only, not investment
advice, capital at risk, past performance is not indicative of future results.
""",
    },
    {
        "source_key": "banking_policies",
        "external_id": "POL-AI-020",
        "title": "AI Agent Usage & Model Risk Policy",
        "author": "Model Risk Management",
        "content": """# AI Agent Usage & Model Risk Policy

## Purpose
Defines the controls under which generative AI agents may operate on bank systems and data.

## Approved use
Agents may retrieve information, analyse data and draft output. Agents may not take a final
customer-impacting or regulator-facing action without human approval. Actions that always require
approval include: escalation to a specialist desk, KYC onboarding decisions, SAR drafting and
filing, case closure, and publication of research.

## Model governance
- Every model in use is registered with its provider, version, context window and pricing.
- Model selection is logged per call along with tokens, latency and cost.
- Fallback to an alternative model is logged and visible in the trace.
- No customer PII may be sent to a provider that is not covered by an approved data processing
  agreement.

## Prompt and output controls
System prompts are versioned and changes are auditable. Outputs pass a guardrail stage that masks
PII, blocks prohibited terms and appends mandated disclaimers. Prompt injection attempts are
detected and recorded against the execution.

## Evaluation
Agents are evaluated on faithfulness, groundedness, hallucination rate, citation coverage and
tool success. Human feedback is collected on a sample of executions. A material regression blocks
promotion of a new agent version.

## Monitoring
Cost, latency, error rate and approval volumes are monitored per agent with alerting on budget
and SLA breach. All executions are traceable end to end for a minimum of 400 days.
""",
    },
]
