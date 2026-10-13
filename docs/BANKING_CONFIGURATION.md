# Sample enterprise configuration for banking

What ships in the box, and what to replace before production.

## Loaded by `finops init-db`

Always installed — this is the platform's baseline configuration.

**Identities** — five role-representative accounts (admin, operator, approver, auditor,
builder) sharing `BOOTSTRAP_ADMIN_PASSWORD`. Change it and remove the ones you do not
need before granting access.

**Agents** — the five implemented agents plus ten roadmap entries marked `coming_soon`.

**Knowledge corpus** — ten policy and operations documents forming a working baseline:

| Document | Source | Covers |
|---|---|---|
| Customer Due Diligence Policy (CDD-001) | compliance | OVD requirements, verification thresholds, risk classification, EDD triggers, prohibited relationships |
| AML Transaction Monitoring & SAR Policy (AML-002) | compliance | the seven monitoring rules, alert handling, SAR standard, tipping-off, retention |
| Financial Crime Typology Library | compliance | structuring, pass-through, TBML, layering, mule networks, dormant reactivation, sanctions evasion |
| Customer Servicing & Escalation Policy | banking | authentication before disclosure, data minimisation, escalation triggers, SLAs, unauthorised transactions |
| Retail Product Catalogue | banking | deposits, cards, loans and wealth products with real terms |
| Incident Management Runbook | operations | severity classification, on-call, agent-platform triage, rollback |
| Backup, Restore and DR Runbook | operations | RPO/RTO, schedules, restore procedure, retention |
| AI Agent Platform Architecture | engineering | components, state, failure handling, security model |
| Investment Research Methodology | research | research standard, valuation approach, risk framework, position sizing |
| AI Agent Usage & Model Risk Policy | governance | approved use, model governance, prompt controls, evaluation, monitoring |

**Watchlist** — ten internal and PEP entries so screening is functional immediately.
Replace with authoritative lists:

```bash
python -m app.cli load-sanctions        # OFAC SDN + alternate names, public data
```

Add UN, EU and internal lists through the same table (`sanctions_entries`), keyed by
`list_name`.

**Feature flags** — streaming responses, LLM-judge evaluation, automatic evaluation,
agent builder exposure, hybrid retrieval, cost hard stop.

## Loaded by `finops seed-banking` (opt-in)

A representative retail bank so the agents have something real to operate on. Do not run
it in production.

| Table | Content |
|---|---|
| `customers` | 24 customers across retail, premier, private and SME segments with KYC status, risk rating and telephone-banking credentials |
| `accounts` | savings and current accounts with balances and branch details |
| `transactions` | ~1,400 rows over 120 days across UPI, NEFT, IMPS, card, ATM and RTGS, including deliberate structuring and pass-through patterns so monitoring produces genuine hits |
| `cards` | credit cards with limits, statements, dues, APR and reward points |
| `loans` | personal, home, auto and education loans including delinquent cases |
| `faq_entries` | seven curated servicing answers |
| `securities` | eight NSE instruments with fundamentals |
| `price_bars` | ~2,300 daily bars driven by a geometric random walk |
| `portfolios` / `holdings` | one balanced mandate with six positions |
| `kyc_cases` | one in-progress onboarding case |

The generator is seeded, so the dataset is reproducible.

## What to replace before production

| Component | Replace with |
|---|---|
| Banking tables | your core banking system, via replication or a read-only view |
| Watchlist | your screening vendor feed (Dow Jones, Refinitiv, LexisNexis) or the official lists |
| Knowledge corpus | your Confluence, SharePoint and policy repositories through the connectors |
| FAQ entries | your servicing knowledge base |
| Instruments and prices | your market-data vendor via `MARKET_DATA_API_KEY` |
| Bootstrap identities | Keycloak SSO with your directory groups mapped to roles |

## Tuning for your institution

**Thresholds** — `STRUCTURING_THRESHOLD` in `app/tools/aml.py` defaults to INR 1,000,000.
Set it to your jurisdiction's reporting threshold. Rule severities and scores are in the
same file and should reflect your risk appetite.

**Risk model** — weights in `calculate_kyc_risk_score` (sanctions 60, PEP 25, geography 20,
biometric 18, occupation 15, documents 12/failure, address 8) are a defensible starting
point. Calibrate against your existing model and record the rationale for audit.

**Approval policy** — which tools require approval is declared per tool, and the final
approval gate is per agent with a risk threshold. Regulated actions — KYC decisions, SAR
drafting, case closure, escalation, research publication — ship gated. Keep them that way.

**Budgets** — set `DAILY_COST_BUDGET_USD` and `MONTHLY_COST_BUDGET_USD` per business unit
and `PER_EXECUTION_COST_CAP_USD` to something a runaway loop cannot exceed meaningfully.

**Retention** — `DATA_RETENTION_DAYS` defaults to 400. KYC reports and SARs are stored as
artifacts and are exempt from the routine purge; they follow the statutory schedule
(5 years in most jurisdictions).

## Regulatory alignment

The defaults reflect common requirements in Indian retail banking (RBI KYC Master
Direction, PMLA reporting to FIU-IND) and FATF recommendations: authentication before
disclosure, zero liability for unauthorised transactions reported within three working
days, five-year record retention, MLRO sign-off on SARs, and the tipping-off prohibition
enforced through the AML agent's mandatory disclaimer.

None of this constitutes legal advice. Have compliance review the configuration against
your obligations before go-live.
