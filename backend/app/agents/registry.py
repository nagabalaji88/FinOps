"""Agent catalogue: five fully implemented agents plus the published roadmap."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentSpec
from app.core.errors import NotFoundError

# --------------------------------------------------------------------------- #
# 1. Customer Service                                                          #
# --------------------------------------------------------------------------- #
CUSTOMER_SERVICE = AgentSpec(
    key="customer_service",
    name="Customer Service Agent",
    description="Authenticates retail customers and resolves account, card, loan and servicing "
                "queries with sentiment-aware escalation and full audit trail.",
    category="Retail Banking",
    system_prompt="""You are the Customer Service Agent for FinOps Bank, operating inside a
regulated retail banking environment.

Operating rules, in priority order:
1. SECURITY FIRST. Never disclose any account, card, loan or transaction detail until
   `authenticate_customer` has returned authenticated=true in this conversation. If
   authentication fails, explain what the customer must provide; after three failures
   tell them the profile is locked and escalate.
2. USE TOOLS, NEVER GUESS. Every factual statement about balances, limits, dues, dates
   or transactions must come from a tool result in this conversation. If a tool fails or
   returns nothing, say so plainly. Never estimate or illustrate a number.
3. Run `detect_sentiment` on the customer's message early. If it recommends escalation, or
   the customer alleges fraud, requests a complaint, or mentions the ombudsman, create a
   ticket and escalate.
4. Never reveal full card numbers, full account numbers, PANs or Aadhaar numbers. Masked
   values from tools are already safe to repeat.
5. Be concise and warm. Lead with the answer, then the supporting detail. Use the
   customer's currency and local date format.
6. Close by confirming the next step and any ticket number you created.""",
    tools=[
        "authenticate_customer",
        "lookup_customer_accounts",
        "get_account_balance",
        "get_recent_transactions",
        "get_credit_card_details",
        "get_loan_details",
        "search_faq",
        "search_knowledge_base",
        "detect_sentiment",
        "create_support_ticket",
        "escalate_to_human",
    ],
    knowledge_sources=["banking_policies", "product_catalogue"],
    temperature=0.15,
    max_iterations=10,
    memory_enabled=True,
    memory_window=14,
    mask_pii=True,
    final_approval_required=False,
    cost_cap_usd=1.0,
    sla_latency_ms=25_000,
    owner="Retail Banking Operations",
    owner_email="retail.ops@finops.local",
    department="Retail Banking",
    tags=["customer", "servicing", "tier-1"],
    input_schema={
        "query": {"type": "string", "required": True, "label": "Customer message"},
        "identifier": {"type": "string", "required": False,
                       "label": "Customer number / email / phone"},
        "pin": {"type": "password", "required": False, "label": "Telephone banking PIN"},
        "thread_id": {"type": "string", "required": False, "label": "Conversation thread"},
    },
    example_input={
        "query": "What is the balance on my savings account and when is my card payment due?",
        "identifier": "CUS-100001",
        "pin": "4821",
    },
)

# --------------------------------------------------------------------------- #
# 2. KYC & Onboarding                                                          #
# --------------------------------------------------------------------------- #
KYC_ONBOARDING = AgentSpec(
    key="kyc_onboarding",
    name="KYC & Customer Onboarding Agent",
    description="Runs document OCR, identity verification, biometric match, address validation, "
                "sanctions/PEP screening and risk scoring, then produces the onboarding report.",
    category="Compliance",
    system_prompt="""You are the KYC & Onboarding Agent for FinOps Bank. You execute customer due
diligence to RBI/FATF standards.

Mandatory sequence for every case:
1. `ocr_document` then `classify_document` for each uploaded document.
2. Verify each identity document with `verify_passport`, `verify_pan` and `verify_aadhaar` as
   applicable. Report every failed check explicitly - never soften a failure.
3. `face_match` between the selfie and the photo identity document.
4. `validate_address` against the address proof.
5. `screen_sanctions` and `screen_pep` on the applicant's exact legal name, plus any alias
   found in the documents.
6. `calculate_kyc_risk_score`.
7. `generate_onboarding_report` with a decision of approve, reject or refer.

Decision policy: any confirmed sanctions hit is an automatic reject. A PEP hit or a risk band
of high or critical means refer for enhanced due diligence - never approve. Failed document
checks or a face-match score below 0.75 mean refer at best.

Output must be evidence-led: cite the specific check, the value observed and the threshold
applied. Do not invent document contents; if OCR is empty, say the document is unreadable and
request a re-upload.""",
    tools=[
        "ocr_document",
        "classify_document",
        "verify_passport",
        "verify_pan",
        "verify_aadhaar",
        "face_match",
        "validate_address",
        "screen_sanctions",
        "screen_pep",
        "calculate_kyc_risk_score",
        "generate_onboarding_report",
        "search_knowledge_base",
    ],
    knowledge_sources=["compliance_policies"],
    temperature=0.05,
    max_iterations=16,
    max_tokens=6000,
    strict_validation=False,
    mask_pii=True,
    pii_allowlist=[],
    final_approval_required=True,
    final_approval_risk_threshold="medium",
    approval_role="approver",
    cost_cap_usd=3.0,
    sla_latency_ms=180_000,
    owner="Financial Crime Compliance",
    owner_email="fcc@finops.local",
    department="Compliance",
    tags=["kyc", "onboarding", "regulated"],
    input_schema={
        "case_number": {"type": "string", "required": True, "label": "KYC case number"},
        "query": {"type": "string", "required": False, "label": "Instruction"},
        "occupation": {"type": "string", "required": False, "label": "Declared occupation"},
        "annual_income": {"type": "number", "required": False, "label": "Declared annual income"},
        "expected_monthly_volume": {"type": "number", "required": False,
                                    "label": "Expected monthly volume"},
    },
    example_input={
        "case_number": "KYC-2026-0001",
        "query": "Complete customer due diligence and recommend a decision.",
        "occupation": "Software engineer",
        "annual_income": 2400000,
        "expected_monthly_volume": 150000,
    },
)

# --------------------------------------------------------------------------- #
# 3. AML Fraud Investigation                                                   #
# --------------------------------------------------------------------------- #
AML_INVESTIGATION = AgentSpec(
    key="aml_investigation",
    name="AML Fraud Investigation Agent",
    description="Monitors transactions for financial-crime typologies, profiles customers, "
                "builds case timelines and evidence, and drafts regulator-ready SARs.",
    category="Financial Crime",
    system_prompt="""You are the AML Investigation Agent for FinOps Bank, supporting the MLRO.

Investigation protocol:
1. `monitor_transactions` for the customer or population in scope.
2. `profile_customer` to establish the expected behaviour baseline.
3. `build_case_timeline` to place alerts and transactions in sequence.
4. `create_investigation_case` when the evidence supports an investigation, then
   `attach_case_evidence` for each analytical finding with the underlying figures.
5. `generate_sar` only when the suspicion is articulable. The narrative must cover who, what,
   when, where, why suspicious, and how the funds moved, quoting exact amounts, dates,
   references and counterparties from tool output.
6. `close_investigation_case` with a disposition and rationale.

Standards: distinguish observation from inference. Never assert criminality - describe activity
inconsistent with the known profile. Quantify everything. If the evidence does not support a
SAR, say so and record a no-further-action disposition with reasoning. Never tip off the
customer; your output is for internal compliance use only.""",
    tools=[
        "monitor_transactions",
        "profile_customer",
        "build_case_timeline",
        "create_investigation_case",
        "attach_case_evidence",
        "generate_sar",
        "close_investigation_case",
        "screen_sanctions",
        "screen_pep",
        "search_knowledge_base",
    ],
    knowledge_sources=["compliance_policies", "aml_typologies"],
    temperature=0.1,
    max_iterations=18,
    max_tokens=8000,
    mask_pii=False,
    final_approval_required=True,
    final_approval_risk_threshold="medium",
    approval_role="approver",
    cost_cap_usd=4.0,
    sla_latency_ms=300_000,
    owner="Financial Crime Operations",
    owner_email="mlro@finops.local",
    department="Compliance",
    tags=["aml", "sar", "investigation"],
    required_disclaimer="Internal compliance work product - confidential. Do not disclose to the "
                        "customer (tipping-off offence).",
    input_schema={
        "customer_id": {"type": "string", "required": False, "label": "Customer id"},
        "query": {"type": "string", "required": True, "label": "Investigation instruction"},
        "days": {"type": "number", "required": False, "label": "Lookback window (days)"},
    },
    example_input={
        "query": "Investigate recent activity for structuring and pass-through behaviour, and "
                 "open a case if warranted.",
        "days": 120,
    },
)

# --------------------------------------------------------------------------- #
# 4. Investment Research                                                       #
# --------------------------------------------------------------------------- #
INVESTMENT_RESEARCH = AgentSpec(
    key="investment_research",
    name="Investment Research Agent",
    description="Produces evidence-based investment research: market data, filings, portfolio "
                "and risk analytics, sector comparison and macro context.",
    category="Wealth & Markets",
    system_prompt="""You are the Investment Research Agent for FinOps Bank's wealth division.

Research protocol:
1. Establish the facts with tools before forming a view: `get_market_data`,
   `analyse_financial_statements`, `get_company_filings`, `compare_sector`,
   `get_macro_indicators`, `analyse_portfolio`, `analyse_portfolio_risk`.
2. State the numbers you relied on, with their source field, in the body of the answer.
3. Separate observation, analysis and recommendation into clearly labelled sections.
4. Every recommendation needs: thesis, valuation anchor, catalysts, at least two downside
   risks, and a time horizon.
5. Use `publish_research_note` only when the user asks for a formal note.

Constraints: never fabricate a price, ratio, filing or news item. If a data source is
unavailable, state the gap and how it limits the conclusion. Distinguish clearly between what
the data shows and what you infer.""",
    tools=[
        "get_market_data",
        "get_market_news",
        "get_company_filings",
        "analyse_portfolio",
        "analyse_portfolio_risk",
        "compare_sector",
        "analyse_financial_statements",
        "get_macro_indicators",
        "publish_research_note",
        "search_knowledge_base",
        "summarise_document",
    ],
    knowledge_sources=["research_library"],
    temperature=0.25,
    max_iterations=14,
    max_tokens=8000,
    require_citations=False,
    final_approval_required=False,
    cost_cap_usd=3.0,
    sla_latency_ms=180_000,
    owner="Investment Research Desk",
    owner_email="research@finops.local",
    department="Wealth Management",
    tags=["research", "markets", "portfolio"],
    required_disclaimer="This material is for information only and is not investment advice, an "
                        "offer, or a solicitation. Capital at risk. Past performance does not "
                        "indicate future results.",
    input_schema={
        "query": {"type": "string", "required": True, "label": "Research question"},
        "symbol": {"type": "string", "required": False, "label": "Ticker"},
        "portfolio_code": {"type": "string", "required": False, "label": "Portfolio code"},
    },
    example_input={
        "query": "Assess the risk profile of this portfolio and whether the technology weight "
                 "should be trimmed.",
        "portfolio_code": "PF-BALANCED-01",
    },
)

# --------------------------------------------------------------------------- #
# 5. Internal Knowledge Assistant                                              #
# --------------------------------------------------------------------------- #
KNOWLEDGE_ASSISTANT = AgentSpec(
    key="knowledge_assistant",
    name="Internal Knowledge Assistant",
    description="Enterprise RAG across policies, architecture documents, runbooks, meeting notes, "
                "Jira, Confluence, SharePoint, Slack and Teams, with mandatory citations.",
    category="Enterprise Productivity",
    system_prompt="""You are the Internal Knowledge Assistant for FinOps Bank employees.

Answering protocol:
1. Always call `search_knowledge_base` before answering. Widen or rephrase the query and search
   again if the first pass is thin.
2. Answer ONLY from retrieved content. Cite every factual sentence with the bracketed marker of
   its source, e.g. [1]. Multiple sources per sentence are fine.
3. If the corpus does not contain the answer, say exactly that and list the closest documents
   found plus who owns them. Never fill the gap from general knowledge.
4. Note conflicts between sources rather than silently picking one, and prefer the most recently
   updated document.
5. Respect classification: flag when the answer draws on documents marked confidential or
   restricted.
6. Structure: direct answer first, supporting detail second, then a Sources list.""",
    tools=[
        "search_knowledge_base",
        "list_knowledge_sources",
        "fetch_document",
        "knowledge_base_stats",
        "summarise_document",
    ],
    knowledge_sources=[
        "engineering_docs", "banking_policies", "compliance_policies", "runbooks",
        "meeting_notes", "product_catalogue",
    ],
    temperature=0.1,
    max_iterations=8,
    retrieval_top_k=8,
    memory_enabled=True,
    require_citations=True,
    final_approval_required=False,
    cost_cap_usd=1.5,
    sla_latency_ms=45_000,
    owner="Platform Engineering",
    owner_email="platform@finops.local",
    department="Technology",
    tags=["rag", "knowledge", "productivity"],
    input_schema={
        "query": {"type": "string", "required": True, "label": "Question"},
        "sources": {"type": "array", "required": False, "label": "Restrict to sources"},
        "thread_id": {"type": "string", "required": False, "label": "Conversation thread"},
    },
    example_input={"query": "What is our incident severity classification and who declares a Sev-1?"},
)

IMPLEMENTED: list[AgentSpec] = [
    CUSTOMER_SERVICE,
    KYC_ONBOARDING,
    AML_INVESTIGATION,
    INVESTMENT_RESEARCH,
    KNOWLEDGE_ASSISTANT,
]

# --------------------------------------------------------------------------- #
# Roadmap agents - registered, listed in the UI, explicitly not executable      #
# --------------------------------------------------------------------------- #
ROADMAP: list[dict[str, Any]] = [
    {"key": "credit_risk", "name": "Credit Risk Agent", "category": "Risk",
     "description": "Underwriting decisions, PD/LGD modelling, limit setting and covenant "
                    "monitoring.",
     "owner": "Credit Risk", "department": "Risk", "planned_quarter": "Q3 2026"},
    {"key": "trading", "name": "Trading Agent", "category": "Markets",
     "description": "Execution strategy selection, TCA and pre-trade compliance checks.",
     "owner": "Markets Technology", "department": "Global Markets", "planned_quarter": "Q4 2026"},
    {"key": "legal_contract", "name": "Legal Contract Agent", "category": "Legal",
     "description": "Contract review, clause extraction, obligation tracking and playbook "
                    "deviation detection.",
     "owner": "Legal Operations", "department": "Legal", "planned_quarter": "Q3 2026"},
    {"key": "compliance", "name": "Compliance Agent", "category": "Compliance",
     "description": "Regulatory change monitoring, control testing and attestation workflows.",
     "owner": "Compliance Assurance", "department": "Compliance", "planned_quarter": "Q4 2026"},
    {"key": "financial_planning", "name": "Financial Planning Agent", "category": "Wealth",
     "description": "Goal-based planning, cash-flow projection and retirement modelling.",
     "owner": "Wealth Advisory", "department": "Wealth Management", "planned_quarter": "Q1 2027"},
    {"key": "software_engineering", "name": "Software Engineering Agent", "category": "Technology",
     "description": "Code review, migration assistance, test generation and incident triage.",
     "owner": "Platform Engineering", "department": "Technology", "planned_quarter": "Q3 2026"},
    {"key": "treasury", "name": "Treasury Agent", "category": "Treasury",
     "description": "Liquidity forecasting, FX exposure management and intraday cash positioning.",
     "owner": "Group Treasury", "department": "Treasury", "planned_quarter": "Q1 2027"},
    {"key": "payment", "name": "Payment Agent", "category": "Payments",
     "description": "Payment investigation, repair, sanctions hit resolution and returns "
                    "handling.",
     "owner": "Payment Operations", "department": "Operations", "planned_quarter": "Q4 2026"},
    {"key": "collections", "name": "Collections Agent", "category": "Retail Banking",
     "description": "Delinquency outreach sequencing, hardship assessment and promise-to-pay "
                    "tracking.",
     "owner": "Collections", "department": "Retail Banking", "planned_quarter": "Q1 2027"},
    {"key": "risk_management", "name": "Risk Management Agent", "category": "Risk",
     "description": "Enterprise risk aggregation, scenario analysis and limit breach escalation.",
     "owner": "Enterprise Risk", "department": "Risk", "planned_quarter": "Q2 2027"},
]


class AgentRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {spec.key: spec for spec in IMPLEMENTED}
        self._overrides: dict[str, AgentSpec] = {}

    def get(self, key: str) -> AgentSpec:
        spec = self._overrides.get(key) or self._specs.get(key)
        if spec is None:
            raise NotFoundError(f"Agent '{key}' is not implemented",
                                details={"implemented": sorted(self._specs)})
        return spec

    def base(self, key: str) -> AgentSpec:
        spec = self._specs.get(key)
        if spec is None:
            raise NotFoundError(f"Agent '{key}' is not implemented")
        return spec

    def has(self, key: str) -> bool:
        return key in self._specs or key in self._overrides

    def all(self) -> list[AgentSpec]:
        return [self._overrides.get(s.key, s) for s in self._specs.values()]

    def apply_override(self, key: str, config: dict[str, Any]) -> AgentSpec:
        """Overlay a stored/edited configuration on a built-in agent."""
        base = self._specs.get(key)
        if base is None:
            spec = AgentSpec(
                key=key,
                name=config.get("name", key),
                description=config.get("description", ""),
                category=config.get("category", "Custom"),
                system_prompt=config.get("system_prompt", "You are a helpful enterprise agent."),
            )
        else:
            spec = base
        merged = AgentSpec.from_config(spec, config)
        self._overrides[key] = merged
        return merged

    def register_custom(self, spec: AgentSpec) -> AgentSpec:
        self._specs[spec.key] = spec
        return spec

    def clear_override(self, key: str) -> None:
        self._overrides.pop(key, None)


agent_registry = AgentRegistry()
