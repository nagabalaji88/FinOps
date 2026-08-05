# FinOps — Enterprise AI Agent Command Center

A control plane for running AI agents against real banking systems: orchestration,
human-in-the-loop approvals, OpenTelemetry-style tracing, cost attribution, RBAC and audit.

Five agents are implemented end to end. Ten more are registered and clearly marked
**Coming soon** — the platform refuses to execute them rather than pretending.

---

## What is actually implemented

| Capability | Status |
|---|---|
| Execution engine (planner → retriever → memory → LLM ↔ tools → validation → guardrails → approval → response) | Working, with per-node spans, replayable event log and checkpointed suspend/resume |
| Model router across OpenAI, Azure OpenAI, Anthropic, Gemini, Bedrock, Mistral, DeepSeek, Together, Ollama | Working, with circuit breakers, retry, fallback chain and per-call cost metering |
| 40 tools over the banking system of record | Working — schema-validated, timed, health-tracked, approval-gated where it matters |
| Hybrid RAG (vector + BM25) with citations | Working — Qdrant when configured, exact cosine in-database otherwise |
| Human approvals that suspend and resume real executions | Working, with segregation of duties and a full decision timeline |
| Cost ledger by agent / model / provider / user / department / tool | Working, with forecasting and budget alerts |
| RBAC, API keys, MFA (TOTP), Keycloak SSO, audit trail, secrets, feature flags | Working |
| Observability: OTel spans, Prometheus metrics, structured correlated logs, SSE/WebSocket streaming | Working |
| React 19 console: 17 pages, live trace waterfall, React Flow DAG, cost and security dashboards | Working |
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
| Console | http://localhost:8080 |
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

# Frontend
cd frontend && npm install && npm run dev      # http://localhost:5173
```

SQLite is the default so a laptop needs no infrastructure. Point `DATABASE_URL` at
PostgreSQL for anything beyond development.

### Run an agent from the CLI

```bash
cd backend
.venv/bin/python -m app.cli run-agent knowledge_assistant \
  --input '{"query":"What is our incident severity classification?"}'
```

---

## The five implemented agents

| Agent | What it does | Tools | Approval gate |
|---|---|---|---|
| **Customer Service** | Authenticates a customer, then answers on accounts, balances, transactions, cards and loans; detects sentiment, raises tickets, escalates | 11 | Escalation |
| **KYC & Onboarding** | OCR → document classification → passport MRZ / PAN / Aadhaar verification → face match → address validation → sanctions & PEP screening → risk score → onboarding report | 12 | Final decision and report |
| **AML Investigation** | Runs seven typology rules over the ledger, profiles the customer, builds a case timeline, collects evidence, drafts a SAR | 10 | SAR drafting, case closure |
| **Investment Research** | Market data, SEC filings, portfolio valuation, historical VaR / expected shortfall / beta, sector comparison, fundamentals, macro indicators | 11 | Publishing a research note |
| **Internal Knowledge Assistant** | Enterprise RAG with mandatory citations across policies, runbooks, architecture docs, Jira, Confluence, SharePoint, Slack, Teams | 5 | — |

Identity checks use the published algorithms: ICAO 9303 MRZ check digits, the Verhoeff
checksum for Aadhaar, and ITD structure rules for PAN — not approximations.

### Roadmap agents (registered, not executable)

Credit Risk · Trading · Legal Contract · Compliance · Financial Planning ·
Software Engineering · Treasury · Payment · Collections · Risk Management

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

Twenty scenario inputs across the five agents, plus an execution validation agent that
routes each one to its agent, runs it for real, reviews any approval gate it hits, and
asserts the expected behaviour.

```bash
cd backend
.venv/bin/python -m app.cli validate                        # all 20
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
cd backend && .venv/bin/pytest -q       # 90 unit, integration and conformance tests
cd frontend && npm run test && npm run typecheck && npm run build
```

The integration suite drives the whole graph through a scripted provider double: tool
calling, approval suspend/resume, guardrail PII masking, trace and event assertions.
The double lives in `tests/conftest.py` and is never registered by the application. The
conformance harness is itself tested — every assertion is proven to fail when it should,
and the runner is exercised end to end including a caught hallucination.

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — engine, router, RAG, state and failure handling
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Compose, Kubernetes, Helm, scaling, backup and restore
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — runbooks, alerts, incident response, retention
- [`docs/SECURITY.md`](docs/SECURITY.md) — RBAC matrix, auth flows, secrets, audit, data handling
- [`docs/AGENTS.md`](docs/AGENTS.md) — agent contracts, tool catalogue, building a new agent
- [`docs/API.md`](docs/API.md) — endpoint reference and streaming protocol
- [`docs/VALIDATION.md`](docs/VALIDATION.md) — the 20 scenarios, assertions and the validation agent
- [`docs/BANKING_CONFIGURATION.md`](docs/BANKING_CONFIGURATION.md) — sample enterprise configuration

---

## Repository layout

```
backend/     FastAPI app, execution engine, agents, tools, RAG, migrations, tests
frontend/    React 19 + TypeScript console
infra/       Kubernetes, Helm, Prometheus, Grafana, OpenTelemetry, nginx
docs/        Architecture, deployment, operations, security, API
```

## Licence

Proprietary — internal enterprise deployment.
