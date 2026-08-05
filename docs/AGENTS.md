# Agents and tools

## Agent contract

An agent is an `AgentSpec` (`backend/app/agents/base.py`) — a declaration, not code. The
shared graph executes it, so every agent gets the same tracing, budgets, guardrails,
approvals and audit.

```python
AgentSpec(
    key="treasury_liquidity",
    name="Treasury Liquidity Agent",
    description="Forecasts intraday liquidity and flags buffer breaches.",
    category="Treasury",
    system_prompt="...",              # operating rules, in priority order
    tools=["get_account_balance", "search_knowledge_base"],
    knowledge_sources=["runbooks"],
    model=None,                        # None lets the router decide
    temperature=0.15,
    max_iterations=10,
    memory_enabled=False,
    require_citations=False,
    mask_pii=True,
    final_approval_required=True,
    final_approval_risk_threshold="high",
    cost_cap_usd=2.0,
    input_schema={"query": {"type": "string", "required": True, "label": "Request"}},
)
```

Configuration is overridable at runtime. `PUT /api/v1/agents/{key}/config` creates a new
version; publishing makes it live; rollback restores any earlier version. The engine reads
the published configuration on every execution, so changes take effect without a redeploy.

### Writing a system prompt that holds up

The implemented agents follow the same pattern, and it is the pattern to copy:

1. **State the operating rules in priority order.** Security first, then "use tools, never
   guess", then domain policy.
2. **Forbid invention explicitly.** "Every factual statement must come from a tool result
   in this conversation. If a tool fails, say so plainly."
3. **Prescribe the sequence** where order is a control — the KYC agent's OCR → verify →
   screen → score → report sequence is a compliance requirement, not a suggestion.
4. **Define the decision policy.** What forces a reject, what forces a refer, what may be
   approved.
5. **Set the output shape.** Answer first, then evidence, then sources.

## Tool contract

```python
class BalanceArgs(BaseModel):
    account_id: str | None = Field(default=None, description="Specific account id")

@tool(
    "get_account_balance",
    "Return current, available and held balances for the customer's accounts.",
    BalanceArgs,
    category="banking",
    timeout_seconds=30,
    max_retries=2,
    idempotent=True,
    writes_data=False,
    requires_approval=False,
)
async def get_account_balance(args: BalanceArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx)
    ...
```

Rules that keep tools safe:

- **Validate with Pydantic.** The schema is what the model sees; a bad argument returns a
  structured error, not an exception.
- **Mark writes.** `writes_data=True` disables retry unless the operation is genuinely
  idempotent, and surfaces in the approval payload.
- **Gate consequential actions.** `requires_approval=True` suspends the execution. Use it
  for anything a customer or regulator would see: escalation, KYC decisions, SAR drafting,
  case closure, research publication.
- **Return data, not prose.** The model composes the narrative; the tool returns facts.
- **Fail loudly.** Raise `NotFoundError` or `ValidationError` with details rather than
  returning an empty result that reads like a real answer.

## Tool catalogue

**Customer servicing (banking)** — `authenticate_customer`, `lookup_customer_accounts`,
`get_account_balance`, `get_recent_transactions`, `get_credit_card_details`,
`get_loan_details`, `search_faq`, `create_support_ticket`, `escalate_to_human`†,
`detect_sentiment`

**KYC** — `ocr_document`, `classify_document`, `verify_passport`, `verify_pan`,
`verify_aadhaar`, `face_match`, `validate_address`, `screen_sanctions`, `screen_pep`,
`calculate_kyc_risk_score`, `generate_onboarding_report`†

**AML** — `monitor_transactions`, `profile_customer`, `build_case_timeline`,
`create_investigation_case`, `attach_case_evidence`, `generate_sar`†,
`close_investigation_case`†

**Markets** — `get_market_data`, `get_market_news`, `get_company_filings`,
`analyse_portfolio`, `analyse_portfolio_risk`, `compare_sector`,
`analyse_financial_statements`, `get_macro_indicators`, `publish_research_note`†

**Knowledge** — `search_knowledge_base`, `list_knowledge_sources`, `fetch_document`,
`knowledge_base_stats`, `summarise_document`

† requires human approval

## Domain algorithms

These are implemented to specification, not approximated:

- **ICAO 9303 MRZ** — 7-3-1 weighting; passport number, date of birth, expiry and
  composite check digits all validated. A failed composite digit is a hard failure.
- **Verhoeff** — the Aadhaar checksum, using the standard dihedral, permutation and
  inverse tables.
- **PAN** — ITD structure `AAAAA9999A`, holder-type character decoded, surname initial
  cross-checked against the application.
- **AML typologies** — structuring below the reporting threshold, rapid movement of funds
  (48-hour outflow ratio), high-risk jurisdictions, round-value layering, velocity spikes,
  dormant reactivation, counterparty concentration.
- **Portfolio risk** — historical-simulation VaR and expected shortfall over a rolling
  window, annualised volatility, beta against the mandate benchmark, Herfindahl
  concentration.
- **Name matching** — Unicode normalisation, then a blend of sequence ratio (0.6) and token
  Jaccard (0.4), with a configurable threshold per screening call.

## Building a new agent

Through the console: **Agent Builder** — pick tools and knowledge sources, write the
prompt, set guardrails and approval policy, publish. The agent is executable immediately.

In code, for something that needs custom logic: add an `AgentSpec` to
`backend/app/agents/registry.py`, register any new tools in `backend/app/tools/`, and add
an integration test that drives the graph with the scripted provider from
`tests/conftest.py`.

## Evaluation

Deterministic metrics are always computed: citation coverage, tool success rate and
groundedness (token overlap between the answer and retrieved context). When a provider is
configured, an LLM-as-judge pass adds faithfulness, answer relevance and a hallucination
score. Without one, those fields stay null — they are never filled with a guess.

Human feedback (1–5 with comments) attaches to any execution and rolls up per agent on the
evaluation dashboard. Treat a material regression as a release blocker for a new agent
version.
