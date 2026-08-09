# FinOps — Enterprise AI Agent Command Center

A control plane for running AI agents against real banking systems: orchestration,
human-in-the-loop approvals, OpenTelemetry-style tracing, cost attribution, RBAC and audit.

Seven agents are implemented end to end. Eight more are registered and clearly marked
**Coming soon** — the platform refuses to execute them rather than pretending.

---

## What is actually implemented

| Capability | Status |
|---|---|
| Execution engine (planner → retriever → memory → LLM ↔ tools → validation → guardrails → approval → response) | Working, with per-node spans, replayable event log and checkpointed suspend/resume |
| Model router across OpenAI, Azure OpenAI, Anthropic, Gemini, Bedrock, Mistral, DeepSeek, Together, Ollama | Working, with circuit breakers, retry, fallback chain and per-call cost metering |
| 67 tools over the banking system of record | Working — schema-validated, timed, health-tracked, approval-gated where it matters |
| Hybrid RAG (vector + BM25) with citations | Working — Qdrant when configured, exact cosine in-database otherwise |
| Human approvals that suspend and resume real executions | Working, with segregation of duties and a full decision timeline |
| NeMo Guardrails rails on **all seven** implemented agents (injection, control bypass, unlicensed advice, financial-crime facilitation, tipping off, fair lending, collections conduct, due-diligence integrity, market abuse, corpus exfiltration, PII disclosure) | Working — deterministic rails need no credentials and hold even when the model layer is unreachable; a rail that cannot be evaluated blocks, a provider that cannot be reached degrades |
| Cost ledger by agent / model / provider / user / department / tool | Working, with forecasting and budget alerts |
| Geography: transaction corridors, customer locations and jurisdiction risk on a rotating globe | Working — aggregated from the ledger, coordinates from a static ISO-3166 reference |
| RBAC, API keys, MFA (TOTP), Keycloak SSO, audit trail, secrets, feature flags | Working |
| Observability: OTel spans, Prometheus metrics, structured correlated logs, SSE/WebSocket streaming | Working |
| React 19 front end: two applications — Execute (run agents, read analytics) and the platform console (18 pages: live trace waterfall, React Flow DAG, geography globe, cost and security dashboards) | Working |
| Docker Compose, Kubernetes manifests, Helm chart, GitHub Actions CI | Working |

### What requires configuration to work

The platform never fabricates a result. Where a capability depends on an external
provider, it returns `503 provider_not_configured` naming the exact settings required:

- **Agent execution needs at least one LLM provider key.** Without one, executions fail
  with a clear provider error, and the failure is fully traced.
- **OCR** uses Tesseract if installed, then a vision-capable model. Neither present → explicit error.
- **Face match** requires a vision model or a biometric provider.
- **Market news** requires `NEWS_API_KEY`; live prices require `MARKET_DATA_API_KEY`
  (otherwise the stored price history is used and the response says `"source": "database"`).
- **Jira / Confluence / Slack / SharePoint / Teams / GitHub / S3** connectors are inert
  until their credentials are set; `sync` reports `not_configured` with the missing keys.

Retrieval, screening, transaction monitoring, portfolio analytics, dashboards, RBAC and
audit all work with **zero** external credentials.

---

## Quick start

### Docker Compose (full stack)

```bash
cp .env.example .env          # add at least one provider key
docker compose up -d --build
docker compose exec api python -m app.cli seed-banking   # optional sample bank
```

| Surface | URL |
|---|---|
| Execute (application 1) | http://localhost:8080 |
| Platform console (application 2) | http://localhost:8090 |
| API + OpenAPI | http://localhost:8000/docs |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |
| Jaeger | http://localhost:16686 |
| MinIO console | http://localhost:9001 |

Sign in with `admin@finops.local` and the value of `BOOTSTRAP_ADMIN_PASSWORD`.
**Rotate it before anyone else gets access.**

### Local development

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli seed-banking --customers 24
.venv/bin/uvicorn app.main:app --reload --port 8000

# Front end — one npm workspace, two applications
npm install
npm run dev:execute     # application 1 — http://localhost:5174
npm run dev:console     # application 2 — http://localhost:5173
```

SQLite is the default so a laptop needs no infrastructure. Point `DATABASE_URL` at
PostgreSQL for anything beyond development.

### Check what the model layer can actually do

```bash
cd backend
.venv/bin/python -m app.cli verify-provider
```

Reports which model-dependent paths — a live completion, the LLM-backed rails per agent,
the conformance suite — are verified and which are still unproven, and exits non-zero while
any remain. Everything that needs no model is covered by the test suite instead.

### Run an agent from the CLI

```bash
cd backend
.venv/bin/python -m app.cli run-agent knowledge_assistant \
  --input '{"query":"What is our incident severity classification?"}'
```

---

## Two applications, one backend

The front end ships as two separately deployed applications over the same FastAPI backend,
the same database and the same auth realm. Splitting the deployment, not the system of
record, means a run started in **Execute** is the same execution the **platform console**
traces, prices and audits.

| | **Execute** (application 1) | **Platform console** (application 2) |
|---|---|---|
| Purpose | Operators run agents and read how they are performing | Engineering, risk and compliance run the platform |
| Screens | Login, then a dashboard with exactly two destinations: **Execute** and **Analytics** | 18 pages — agents, executions, approvals, cost, knowledge, tools, services, evaluation, playground, builder, logs, security, geography, settings |
| Agents | **Two on the board at a time**, chosen from the implemented agents and swappable per operator | All fifteen, including the eight marked *Coming soon* |
| Image | `agent-execute` | `agent-console` |
| Dev / Compose port | 5174 / 8080 | 5173 / 8090 |

The Execute screen is a complete execution surface, not a launcher: a form generated from
the agent's own input schema, the live SSE event stream (nodes, tool calls with arguments
and results, model calls, guardrails), the human-approval gate with approve/reject and
segregation of duties enforced, cancellation, and the final response with citations,
structured output, tokens, latency and cost. Analytics reads throughput, success rate,
latency, spend by model and per-tool reliability for the two agents on the board.

Which pair loads by default is a deployment setting (`VITE_EXECUTE_AGENTS`, default
`customer_service,aml_investigation`); an operator can swap either slot for any of the
seven implemented agents and the choice sticks per browser.

Details in [`docs/APPLICATIONS.md`](docs/APPLICATIONS.md).

---

## The seven implemented agents

| Agent | What it does | Tools | Approval gate |
|---|---|---|---|
| **Customer Service** | Authenticates a customer, then answers on accounts, balances, transactions, cards and loans; detects sentiment, raises tickets, escalates | 11 | Escalation |
| **KYC & Onboarding** | OCR → document classification → passport MRZ / PAN / Aadhaar verification → face match → address validation → sanctions & PEP screening → risk score → onboarding report | 12 | Final decision and report |
| **AML Investigation** | Runs seven typology rules over the ledger, profiles the customer, builds a case timeline, collects evidence, drafts a SAR | 10 | SAR drafting, case closure |
| **Investment Research** | Market data, SEC filings, portfolio valuation, historical VaR / expected shortfall / beta, sector comparison, fundamentals, macro indicators | 11 | Publishing a research note |
| **Internal Knowledge Assistant** | Enterprise RAG with mandatory citations across policies, runbooks, architecture docs, Jira, Confluence, SharePoint, Slack, Teams | 5 | — |
| **Credit Risk** | Bureau, FOIR against verified income, logistic scorecard PD, LGD after collateral haircuts, Basel III IRB capital, risk-based pricing, policy knockouts, limit recommendation | 13 | Recording the decision |
| **Collections** | Arrears and RBI asset classification, collectability scoring, Fair Practices Code contact eligibility, hardship affordability, promises to pay, restructuring | 14 | Repayment plan, recovery referral |

Identity checks use the published algorithms: ICAO 9303 MRZ check digits, the Verhoeff
checksum for Aadhaar, and ITD structure rules for PAN — not approximations.

Credit Risk implements the published models rather than approximating them: a logistic
scorecard with points-to-double-the-odds scaling, the RBI's FOIR against income verified
from the customer's own salary credits, and the Basel III IRB capital formula. Collections
implements the RBI asset-classification ladder including the SMA sub-grades, and the Fair
Practices Code contact rules — evaluated in the bank's local time, not UTC. Details in
[`docs/CREDIT_AND_COLLECTIONS.md`](docs/CREDIT_AND_COLLECTIONS.md).

**Every implemented agent** carries a [NeMo Guardrails](docs/GUARDRAILS.md) rail
configuration — an implemented agent with no rails is an ungoverned production surface, and
a test asserts none exists. Input rails refuse a request before anything reads a system of
record; output rails mask identifiers, block tipping off, refuse reasoning from a protected
characteristic, refuse collections threats, refuse a promised return and refuse credential
extraction from the corpus. Rails fail closed — with one deliberate exception: a provider
that is configured but unreachable degrades the model-judged layer rather than blocking all
traffic, because the deterministic rails need no credentials and keep enforcing.

### Roadmap agents (registered, not executable)

Trading · Legal Contract · Compliance · Financial Planning ·
Software Engineering · Treasury · Payment · Risk Management

---

## Architecture

```
React 19 console  ──SSE / WebSocket──▶  FastAPI  ──▶  Execution engine
        │                                  │              │
        │                                  │              ├─ Model router → 9 providers
        │                                  │              ├─ Tool registry → banking tables
        │                                  │              ├─ RAG pipeline → Qdrant / PostgreSQL
        │                                  │              └─ Approval gate → suspend + checkpoint
        │                                  │
        └──────────────────────────────────┴──▶ PostgreSQL · Redis · Kafka · MinIO
                                              ▶ OpenTelemetry · Prometheus · Grafana · Jaeger
```

Every external dependency is optional. Redis falls back to an in-process cache, Qdrant to
exact cosine search in the primary database, MinIO to the local filesystem, Vault to
encrypted database rows, Kafka to the in-process bus. The Connected Services dashboard
reports exactly which path is active.

Details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Conformance suite

Twenty-eight scenario inputs across the seven agents — four each — plus an execution
validation agent that routes each one to its agent, runs it for real, reviews any approval
gate it hits, and asserts the expected behaviour.

One command does everything: it prepares the database and seed data if they are missing,
then runs the inputs one at a time and prints each agent's result as it arrives.

```bash
cd backend
.venv/bin/python -m app.cli validate                        # all 20, prepares if needed
.venv/bin/python -m app.cli validate --agent aml_investigation
.venv/bin/python -m app.cli validate --tag hitl --output report.md
.venv/bin/python -m app.cli validate --fail-on-blocked      # strict CI mode
```

Assertions are structural — which tools ran, whether an approval was raised, whether the
answer is cited, whether a guardrail fired, cost against cap, trace integrity — plus
narrow content checks where a specific fact matters. Verdicts are `passed`, `failed`,
`blocked` (environment gap, e.g. no provider key) or `error`. Details in
[`docs/VALIDATION.md`](docs/VALIDATION.md).

## Testing

```bash
cd backend && .venv/bin/pytest -q       # 265 unit, integration and conformance tests
npm run lint && npm run test && npm run typecheck && npm run build   # both applications
```

The integration suite drives the whole graph through a scripted provider double: tool
calling, approval suspend/resume, guardrail PII masking, trace and event assertions.
The double lives in `tests/conftest.py` and is never registered by the application. The
conformance harness is itself tested — every assertion is proven to fail when it should,
and the runner is exercised end to end including a caught hallucination.

---

## Documentation

**Start here if you are new to the codebase:**
[`docs/CODEBASE_MAP.md`](docs/CODEBASE_MAP.md) — the complete handoff. Every subsystem, the
contracts between them, the invariants, and step-by-step recipes for adding an agent, a tool
or a rail.

- [`docs/APPLICATIONS.md`](docs/APPLICATIONS.md) — the two applications, what each one is for, and how they are deployed
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — engine, router, RAG, state and failure handling
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Compose, Kubernetes, Helm, scaling, backup and restore
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — runbooks, alerts, incident response, retention
- [`docs/SECURITY.md`](docs/SECURITY.md) — RBAC matrix, auth flows, secrets, audit, data handling
- [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) — the NeMo rails on each production agent, and why they fail closed
- [`docs/CREDIT_AND_COLLECTIONS.md`](docs/CREDIT_AND_COLLECTIONS.md) — the lending agents: scorecard, Basel capital, RBI classification and the Fair Practices Code
- [`docs/PROCESS.md`](docs/PROCESS.md) — what each agent does, step by step, and where a human decides
- [`docs/AGENTS.md`](docs/AGENTS.md) — agent contracts, tool catalogue, building a new agent
- [`docs/API.md`](docs/API.md) — endpoint reference and streaming protocol
- [`docs/VALIDATION.md`](docs/VALIDATION.md) — the 28 scenarios, assertions and the validation agent
- [`docs/BANKING_CONFIGURATION.md`](docs/BANKING_CONFIGURATION.md) — sample enterprise configuration

---

## Repository layout

```
backend/            FastAPI app, execution engine, agents, tools, RAG, migrations, tests
apps/execute/       Application 1 — login, Execute, Analytics
apps/console/       Application 2 — everything else
packages/shared/    Design system, API client and stores used by both applications
infra/              Kubernetes, Helm, Prometheus, Grafana, OpenTelemetry, nginx
docs/               Architecture, deployment, operations, security, API
```

## Licence

Proprietary — internal enterprise deployment.
