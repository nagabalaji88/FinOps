# Codebase Map — complete handoff

**Audience: an engineer or model picking this repository up cold and shipping the next
change.** Everything needed to be productive without reading the whole tree is here: what
each subsystem does, the contracts between them, the invariants that must not be broken,
the conventions the code is written to, and step-by-step recipes for the changes you are
most likely to be asked for.

Read this file first. Then read `docs/PROCESS.md` (how a request flows end to end) and the
one domain doc for whatever you are touching.

- Snapshot: 2026-08-09, branch `claude/enterprise-ai-command-center-fuwcla`
- Backend: 27k lines Python, 265 tests, all green
- Frontend: 11k lines TypeScript, lint/typecheck/tests/builds all green
- 7 agents implemented, 8 on the roadmap, 67 tools, 28 conformance scenarios

---

## 1. What this is

An enterprise AI agent platform for a bank. Two React applications over one FastAPI
backend. Agents are **declarative specifications** executed by **one deterministic graph** —
there is no per-agent orchestration code. Adding an agent means adding a spec, its tools,
its rails and its scenarios; the engine, the API, the console and the trace viewer all pick
it up with no further changes.

**The founding constraint, still in force:** no mock APIs, no dummy responses, no hardcoded
results, no fake execution, no placeholder cards, no artificial delays. Every number on
every screen is computed from the database. A capability that needs an unconfigured
provider **fails loudly** — it never pretends. Roadmap agents refuse to execute with a 422
rather than returning a plausible-looking stub.

When you add something, hold that line. If a feature cannot work without a credential the
environment lacks, make it report that it cannot work.

---

## 2. Repository layout

```
FinOps/
├── apps/
│   ├── execute/            @finops/execute  — operator app: login, Execute, Analytics
│   └── console/            @finops/console  — platform app: 17 routes, governance
├── packages/shared/        @finops/shared   — API client, design system, stores, Login
├── backend/
│   ├── app/
│   │   ├── agents/         AgentSpec + the registry of 7 implemented + 8 roadmap
│   │   ├── api/v1/         7 route modules → one api_router
│   │   ├── core/           config, security, rbac, errors, resilience, bus, metrics, otel
│   │   ├── db/models/      agents.py, banking.py, identity.py, knowledge.py
│   │   ├── engine/         executor, nodes (the graph), state, events
│   │   ├── guardrails/     NeMo integration, patterns, actions, configs/<agent>/
│   │   ├── llm/            router, catalog, 4 provider adapters
│   │   ├── rag/            pipeline, vectorstore, embeddings, chunking, connectors
│   │   ├── seed/           corpus, watchlist, geo_reference
│   │   ├── services/       bootstrap (seeding), evaluation, telemetry
│   │   ├── tools/          base (registry) + 7 domain modules = 67 tools
│   │   ├── validation/     conformance harness: scenarios, checks, runner, report
│   │   └── workers/        Celery app + scheduled tasks
│   ├── alembic/versions/   2 migrations
│   └── tests/              5 modules, 265 tests
├── infra/                  helm chart, k8s manifests, prometheus, grafana, otel
├── docs/                   13 documents (this is one)
├── scripts/package.sh      release bundle from `git archive`
└── eslint.config.js        ESLint 9 flat config for the whole workspace
```

---

## 3. The execution graph — the heart of the system

`backend/app/engine/nodes.py` (960 lines). Ten nodes, fixed order, same for every agent:

```
input_rails → planner → retriever → memory → llm ⇄ tools
                                              ↓
                                         validation → guardrails → human_approval → response
```

`GRAPH_DEFINITION` and `GRAPH_EDGES` at the bottom of that file are published verbatim by
`GET /api/v1/agents/graph`, so the React Flow diagram in the console **cannot** drift from
what actually runs. If you add a node, add it in all three places (the `DEFAULT_NODES` list,
`GRAPH_DEFINITION`, `GRAPH_EDGES`) or the diagram lies.

| Node | Class | What it does | Skips when |
|---|---|---|---|
| `input_rails` | `InputRailsNode` | NeMo input rails. Blocks **before** any system of record is touched | no rail config for the agent |
| `planner` | `PlannerNode` | One LLM call producing a JSON plan | never |
| `retriever` | `RetrieverNode` | Hybrid vector + keyword search, builds `retrieval_context` | agent has no `knowledge_sources` |
| `memory` | `MemoryNode` | Loads thread history and durable facts | `memory_enabled` false or no `thread_id` |
| `llm` | `ReasoningNode` | The tool-calling loop, up to `max_iterations` | never |
| `validation` | `ValidationNode` | Schema, citations, groundedness, tool success | never |
| `guardrails` | `GuardrailNode` | PII masking, blocked terms, disclaimer, NeMo output rails | never |
| `human_approval` | `HumanApprovalNode` | Suspends for a reviewer when policy requires | risk below threshold |
| `response` | `ResponseNode` | Finalises output, citations, artifacts, persists memory | never |

`Node.__call__` (nodes.py:134) is the wrapper every node goes through: it appends to
`state.node_path`, opens a span, emits `node.started`/`node.completed`/`node.failed`, and
honours `should_run()` by emitting `node.skipped`. **Never override `__call__`** — override
`run()` and `should_run()`.

### The tool loop

`ReasoningNode.run` (nodes.py:417) is the ReAct loop. Per iteration: call the model with the
agent's tool schemas → if the response has tool calls, execute them (`_execute_tools`,
nodes.py:533) and append observations → repeat. Exits on a text answer, on
`max_iterations`, or on budget exhaustion.

A tool with `requires_approval=True` does **not** execute. `create_approval` (nodes.py:629)
writes an `Approval` row and raises `ApprovalPause`.

### Suspend and resume — get this right

`ApprovalPause` propagates to `ExecutionEngine._run` which:
1. serialises `state.to_checkpoint()` into `Execution.checkpoint`
2. sets status `suspended`
3. emits `execution.suspended` and **closes the SSE stream**

On `POST /approvals/{id}/decision`, `resume_after_approval` restores the state, records the
decision in `approved_tool_calls`, and resumes from `current_node`.

**Invariant:** anything you add to `ExecutionState` that must survive a suspension has to be
added to **both** `to_checkpoint()` and `restore()` in `engine/state.py`. A field added to
only one silently resets to its default halfway through every approved run — the failure
mode is a run that "forgets" something after approval, which is very hard to spot.

**Frontend consequence:** the stream ends at the gate. `apps/execute/src/lib/useAgentRun.ts`
tracks `lastSequence` and re-subscribes with `?after=<sequence>` after the decision. Any new
client that streams executions must do the same or it will show a run that appears to stop
forever.

---

## 4. Agents

### The spec

`backend/app/agents/base.py` — `AgentSpec` is a frozen-in-practice dataclass. Key fields:

| Field | Effect |
|---|---|
| `tools` | Tool names offered to the model. Must exist in the registry |
| `knowledge_sources` | Retrieval scope. Empty means the retriever node skips |
| `max_iterations` | Tool-loop ceiling |
| `require_citations` | Validation fails an uncited answer |
| `output_schema` | Keys the response JSON must contain |
| `mask_pii` / `pii_allowlist` | Output masking |
| `final_approval_required` + `final_approval_risk_threshold` | Whether the whole answer needs sign-off |
| `cost_cap_usd` | Hard budget; the run fails when exhausted |
| `input_schema` | Drives both server-side validation **and** the Execute form UI |

`to_config()` is what gets stored on the `agents` row and rendered in the console's
configuration editor. `build_messages()` composes the system turn from prompt + memory +
retrieval + plan + output schema.

### The registry

`backend/app/agents/registry.py:510` — `IMPLEMENTED` (7 specs) and `ROADMAP` (8 dicts).

Implemented: `customer_service`, `kyc_onboarding`, `aml_investigation`,
`investment_research`, `knowledge_assistant`, `credit_risk`, `collections`.

Roadmap: `trading`, `legal_contract`, `compliance`, `financial_planning`,
`software_engineering`, `treasury`, `payment`, `risk_management`.

**The registry is the single source of truth.** Tests derive from it rather than hardcoding
counts — `tests/test_validation.py` reads `IMPLEMENTED` so that implementing an agent
without adding its scenarios fails the suite, and `tests/test_guardrails.py` asserts every
implemented agent has a rail configuration. Do not "fix" a failing count by editing the
number; the count is telling you something is missing.

---

## 5. Tools

`backend/app/tools/base.py` — 67 tools across 7 domain modules, registered by the `@tool` decorator.

```python
class MyArgs(BaseModel):
    case: str = Field(description="Case reference, e.g. COL-100001")

@tool(
    name="my_tool",
    description="What the model sees. Write it for the model, not for a human reader.",
    args_model=MyArgs,
    category="collections",
    requires_approval=False,     # True suspends the run for a human
    approval_risk="high",        # low | medium | high | critical
    writes_data=True,
    timeout_seconds=30.0,
    max_retries=2,
    idempotent=True,             # False disables retry
)
async def my_tool(args: MyArgs, ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session          # AsyncSession injected by the engine
    ...
```

`registry.invoke()` gives you, for free: argument validation, timeout, retry with backoff, a
circuit breaker per tool, Prometheus metrics, health tracking, and an audit record. A tool
never raises to the caller — failures come back as `ToolResult(ok=False, error=...)` so the
model can react.

Counts by domain: banking 9, kyc 11, aml 7, market 9, credit 12, collections 13, knowledge
5, analysis 1.

**Invariant:** tools are the only path to a system of record. Do not query the database from
a node, a rail or an API handler on an agent's behalf.

---

## 6. Guardrails — two layers, fail closed

Full detail in `docs/GUARDRAILS.md`. The essentials:

**Layer 1, engine rules** (`GuardrailNode`): PII masking, blocked terms, required
disclaimer. Configured per agent on the spec.

**Layer 2, NeMo Guardrails** (`backend/app/guardrails/`): a real NeMo project per agent under
`configs/<agent_key>/{config.yml,rails.co,prompts.yml}`. All 7 implemented agents have one,
and a test asserts none is missing — an implemented agent without rails is an ungoverned
production surface.

- `patterns.py` — the deterministic regexes. No credentials needed, so they hold in every
  environment.
- `actions.py` — `DETECTORS` (judge) and `TRANSFORMS` (rewrite). Detectors are wrapped in
  `fail_closed` **at registration**, not at definition, so a detector added later cannot fail
  open by forgetting the decorator.
- `nemo.py` — loading, caching, degradation.
- `llm_adapter.py` — `RouterLLM` implements NeMo's `LLMModel` protocol over the platform's
  own router, so rail calls are routed, retried, circuit-broken and cost-metered like any
  other call, tagged `purpose: guardrail`.

### Three fail-closed rules and one deliberate exception

Blocked: a rail action raises; the config will not load; an LLM-backed rail errors while its
provider is otherwise healthy.

**Not blocked: a systematically unreachable provider.** `configured` only means the settings
are non-empty — a rotated or placeholder credential would leave every rail call failing and,
because rails fail closed, block **all** traffic on **every** railed agent. That is an
outage, not a control. So availability is judged by `router.usable_providers()` (configured
**and** not circuit-broken); when none is usable the model-judged flows are removed and the
deterministic rails carry on alone. Rails are cached per `(agent, llm_rails_on)` so a
recovering provider does not leave the degraded config in front of it.

### Writing patterns — the recurring bug class

Four separate bugs in this file's history were all the same mistake: a pattern that looked
strict and matched nothing.

- **Inflection.** `\barrest\b` misses "arrested"; `\breturn\b` misses "returns". Use `\w*`
  stems.
- **Order.** In `PII_PATTERNS`, a 16-digit card also satisfies the Aadhaar pattern. Longest
  and most specific first — a test asserts `card_number` precedes `aadhaar`.
- **Subject anchoring.** The fair-lending rail must catch "she is single" but not "a single
  missed instalment", "the age of the credit file", or "community lending scheme". Match
  ambiguous words next to the person, not as bare words.
- **False positives are a defect, not a tuning detail.** `TestFairLendingPatterns` asserts
  twelve prohibited phrasings block **and** thirteen ordinary underwriting sentences do not.
  Copy that structure for any new rail.

---

## 7. LLM layer

`backend/app/llm/router.py` — selection order: explicit model → agent's preferred model →
policy by capability/tier/cost → fallback chain across configured providers.

Providers: OpenAI (and Azure, Mistral, DeepSeek, Together, Ollama through the
OpenAI-compatible adapter), Anthropic, Google, Bedrock. `catalog.py` holds 27 model specs
with real pricing; `compute_cost` meters every call into the cost ledger.

Two methods that mean different things and are easy to confuse:

- `configured_providers()` — settings are present. Says nothing about whether they work.
- `usable_providers()` — configured **and** the `llm:<name>` circuit is not open.

Use `usable_providers()` for any "can we do this?" decision.

---

## 8. Data model

`backend/app/db/models/` — 4 modules, 49 tables.

- **agents.py** — `agents`, `agent_versions`, `executions`, `spans`, `execution_events`,
  `log_records`, `approvals`, `cost_records`, `evaluations`, `playground_runs`,
  `tool_health`, `scheduled_jobs`
- **banking.py** — customers, accounts, transactions, cards, loans, tickets, FAQ; KYC cases
  and documents; sanctions, AML alerts/cases, SAR reports; securities, portfolios, holdings,
  price bars, research notes; and the 7 lending tables (credit applications, bureau records,
  credit decisions, delinquency cases, contact attempts, promises to pay, repayment plans)
- **identity.py** — users, api_keys, refresh_tokens, audit_logs, feature_flags,
  stored_secrets
- **knowledge.py** — knowledge_sources, documents, chunks, search_query_logs,
  memory_threads, memory_messages

### Migrations — the trap that already bit once

The test suite builds its schema with `create_all`, so **a model added without a migration
passes every test and only breaks on a real deployment.** That is exactly what happened with
the seven lending tables. CI now runs `alembic check` as a drift guard.

After any model change:

```bash
cd backend
alembic revision --autogenerate -m "what changed"
# then hand-check the generated file
alembic upgrade head && alembic downgrade base && alembic upgrade head
alembic check          # must say: No new upgrade operations detected
```

Autogenerate omits two imports the custom types need. Add them by hand:

```python
from sqlalchemy import Text
import app.db.base  # noqa: F401 - UTCDateTime is referenced by the column definitions
```

---

## 9. Seeding and the promotion rule

`backend/app/services/bootstrap.py` — `python -m app.cli init-db` creates the schema and
seeds users, agents, feature flags, watchlists and the knowledge corpus.
`seed-banking --customers N` loads the sample bank.

**`init-db` is designed to run on every upgrade, not just on an empty database.** An agent
promoted from `ROADMAP` to `IMPLEMENTED` keeps its placeholder row unless seeding moves it —
`coming_soon`, `disabled`, no config — and the API answers *"Agent 'x' is not implemented
yet"* for a released agent. `seed_agents` promotes those rows and backfills the published
version they never had. It promotes `coming_soon` **only**: an agent an operator deliberately
disabled stays disabled.

Sample data is deterministic by design. `_DELINQUENCY_BAND_START = 5` reserves a customer
band after the credit applicants so the two books cannot collide; the test fixture seeds 12
customers for that reason. If you add a book, reserve a band rather than reusing one.

---

## 10. API surface

`backend/app/api/v1/` — 80 endpoints under `/api/v1`, mounted in `router.py`.

| Module | Prefixes | Notable |
|---|---|---|
| `auth.py` | `/auth` | login, refresh, MFA enrol/verify, API keys, users, roles, SSO |
| `agents.py` | `/agents` | list, graph, roadmap, detail, **execute**, lifecycle, config, versions, publish, rollback |
| `executions.py` | `/executions` | list, detail, trace, graph, events, logs, cancel, **SSE stream**, WebSocket |
| `dashboard.py` | `/dashboard` `/costs` `/platform-metrics` | overview, ai-usage, timeseries, knowledge, geography |
| `operations.py` | `/approvals` `/logs` `/security` | approval decisions, log export/stream, audit, secrets, feature flags |
| `platform.py` | `/tools` `/services` `/knowledge` `/playground` `/evaluations` | tool invoke, connected services, RAG search, model playground |
| `validation.py` | `/validation` | scenarios, run, runs |

Errors are uniform: `AppError` subclasses in `core/errors.py` each carry a `code` and a
status. The client (`packages/shared/src/lib/api.ts`) unwraps them into `ApiError`.

### The SSE contract

`GET /executions/{id}/stream?after=<sequence>` replays from `after` then follows live.

sse-starlette emits **CRLF** frame separators. A parser splitting on `\n\n` matches nothing
and silently shows an empty feed — this was a real bug in the console. `parseSseFrames` uses
`/\r?\n\r?\n/` and has a test for the CRLF case. Do not hand-roll another parser.

---

## 11. Frontend

### Two apps, one backend

| | Execute (`apps/execute`, :5174) | Console (`apps/console`, :5173) |
|---|---|---|
| Audience | Operators running work | Platform owners |
| Routes | `/` Execute, `/analytics` | 17 routes |
| Agents shown | 2, from `VITE_EXECUTE_AGENTS` | all 15 |

`apps/execute/src/lib/board.ts:17` — `VITE_EXECUTE_AGENTS` defaults to
`customer_service,aml_investigation`. **Credit Risk and Collections are fully implemented
and railed but not visible in Execute unless that variable is changed.** That is the "show
two at a time" product decision, not an oversight, but it surprises people.

### The shared package

`packages/shared` — `@finops/shared` exports the API client, the design system
(`components/ui/index.tsx`, 550 lines), the Zustand stores (`useAuth`, `useUi`, `useToasts`),
formatters, and the `Login` page. Both apps import from it; neither duplicates it.

Wire types live in `lib/api.ts`. `EventPayload` is a deliberate `Record<string, any>` with a
single documented eslint-disable: the payload is a union keyed by event type, and a
hand-maintained union that goes stale is worse than an open one because it reads as a
guarantee. Consumers narrow it with the published interfaces — `Citation`,
`GuardrailFinding`, `ValidationFinding`, `PlanStep`, `ToolCallSummary`. Use those rather than
reintroducing `any`.

### The run console

`apps/execute/src/pages/Execute.tsx` + `components/execute/{RunConsole,EventFeed,ApprovalGate}.tsx`
+ `lib/useAgentRun.ts`. Schema-driven form from `input_schema`, live SSE feed, inline
approval gate with separation of duties, cancel, and a result panel with citations and cost.
`nextPhase()` is a pure function and is unit-tested — put run state transitions there rather
than inside the effect.

### Lint

ESLint 9 flat config at the repo root, one file for the whole workspace. `npm run lint` is a
real CI gate at zero warnings. When you hit an `any`, fix the type — the last cleanup removed
28 of them by publishing wire types and consolidating a tooltip that had been copy-pasted
across seven charts into one typed component.

---

## 12. Validation harness

`backend/app/validation/` — 28 scenarios, 4 per implemented agent, run against real
executions.

```bash
cd backend && python -m app.cli validate [--agent KEY] [--fail-on-blocked]
```

Verdicts:

- **passed** — every critical check passed
- **failed** — an agent defect
- **blocked** — an environment gap. A provider error, *or* a refusal carrying `rail_error` /
  `rails_unavailable`, because rails fail closed and a broken provider makes them refuse
  everything. Blocked does not fail the build by default
- **error** — the validator itself could not complete

**A negative rail scenario must name the rule it expects** (`guardrail_rules_expected`).
`status="failed"` alone would also be satisfied by the provider being down, so the scenario
would pass while testing nothing. CR-04 names `prohibited_credit_factor` and passes with no
provider present, on the deterministic rail alone.

Two guards worth knowing because they encode past mistakes:

- `Expectation.__post_init__` rejects a bare string where a tuple is meant.
  `must_match=("a|b")` is a string, and iterating it asserts one character at a time — three
  scenarios shipped like that before the guard existed.
- `tests/test_validation.py` derives coverage from the registry, so a new agent without
  scenarios fails.

---

## 13. Recipes

### Add an agent (the main task ahead)

Work in this order; each step is testable on its own.

1. **Domain tables** — add models to `db/models/banking.py`, then generate and verify a
   migration (§8). Do not skip `alembic check`.
2. **Seed a book** — a `_seed_*` function in `services/bootstrap.py`, reserving its own
   customer band. Deterministic, with a fixed RNG seed.
3. **Tools** — a new `app/tools/<domain>.py`, `@tool` per operation. Anything that writes an
   irreversible decision gets `requires_approval=True`. Import the module in
   `app/tools/__init__.py` or it never registers.
4. **Spec** — an `AgentSpec` in `agents/registry.py`; move the key from `ROADMAP` to
   `IMPLEMENTED`. Write the system prompt to name the tools and the order they should be used.
5. **Rails** — `guardrails/configs/<key>/{config.yml,rails.co,prompts.yml}`. Copy
   `aml_investigation`, which exercises both a blocking and a masking output rail. New
   patterns go in `patterns.py`, detectors in `DETECTORS`.
6. **Scenarios** — 4 in `validation/scenarios.py`: a happy path, a control that must fire, an
   approval gate, and a negative case naming its rule.
7. **Tests** — domain tests like `tests/test_credit_collections.py` (the models are testable
   with no provider), rail tests in `tests/test_guardrails.py` covering both blocked and
   allowed.
8. **Docs** — a domain doc, plus the rail table in `docs/GUARDRAILS.md` and the scenario
   table in `docs/VALIDATION.md`.
9. **Counts** — update `.github/workflows/ci.yml` if it asserts a scenario total.

Credit Risk and Collections are the reference implementations. Read
`docs/CREDIT_AND_COLLECTIONS.md`, `app/tools/credit.py` and `app/tools/collections.py`
before starting — the published models (Basel III IRB capital, a logistic scorecard with PDO
scaling, FOIR affordability, LGD with collateral haircuts, RBI asset classification) are the
standard of rigour expected.

**Suggested next tranche: Payment and Treasury.** Payment reuses the sanctions-screening and
case machinery already built for AML; Treasury reuses the ledger and cash-position tables
from Collections.

### Add a tool to an existing agent

Write it in the domain module, add the name to the spec's `tools`, add a scenario assertion
that it is called. That is all — the schema reaches the model automatically.

### Add a rail

§6 plus `docs/GUARDRAILS.md` §"Adding rails to another agent". Always add the false-positive
half of the test.

---

## 14. Running it

```bash
# Backend
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev,guardrails]"
cp ../.env.example ../.env          # set JWT_SECRET, BOOTSTRAP_ADMIN_PASSWORD, a provider key
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli seed-banking --customers 24
.venv/bin/uvicorn app.main:app --reload --port 8000

# Frontend
npm install
npm run dev:execute      # :5174
npm run dev:console      # :5173

# Everything
docker compose up -d --build
```

Checks before you push:

```bash
cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q   # 265 tests, ~160s
npm run lint && npm run typecheck && npm run test && npm run build
```

`python -m app.cli verify-provider` reports which model-dependent paths are proven and which
are not: exit 0 verified, 1 failed, 2 unproven. In an environment with no working credential
it will report 2 verified, 1 failed, 8 unproven — that is correct and expected, not a
regression.

Config lives in `backend/app/core/config.py`. Every external dependency is optional; an
unconfigured one is reported `not_configured` on Connected Services rather than faked.
`CORS_ORIGINS` takes a **comma-separated** list (it needs `NoDecode` because
pydantic-settings JSON-decodes complex types before validators run — a JSON array works too,
but the comma form is what Helm and Compose produce).

---

## 15. Current state and what is open

**Implemented and proven:** 7 agents, 67 tools, rails on all 7, 28 scenarios, ~90 endpoints,
both applications, Docker Compose with 18 services, Helm chart, CI with 6 jobs.

**Open work, honestly labelled:**

| Item | State |
|---|---|
| 8 roadmap agents | Registered, listed, refuse to execute. Payment + Treasury proposed next |
| `ruff format --check` in CI | `\|\| true`. 60 of 89 files would reformat — cosmetic, but the gate is decorative |
| `mypy` in CI | `\|\| true`, and not installed in the venv. Has never actually run |
| `pip-audit` in CI | `\|\| true`. Dependency CVEs do not fail the build |
| Model-dependent paths | Unproven here — no usable provider credential. `verify-provider` names each one |
| Helm chart | Linted in CI only; no `helm` binary in the dev container |
| Credit Risk / Collections in Execute | Implemented and railed but hidden behind `VITE_EXECUTE_AGENTS` |

---

## 16. Conventions

**Comments explain why, never what.** Every non-obvious line in this codebase carries the
reason it is that way, usually naming the failure it prevents. Match that density — a comment
restating the code is noise, and a tricky line with no comment is a trap for the next reader.

**Tests are written the way a control is tested:** proven to fire on the behaviour it exists
to stop, proven *not* to fire on legitimate work, and proven to fail closed when it cannot
run. A test asserting only the happy path is half a test.

**Assert invariants, not counts.** `len(x) == 7` breaks every time someone ships an agent and
teaches people to edit the number. Derive from the registry.

**Regulatory claims are specific.** The code cites ECOA s.701(a), PMLA s.63, POCA s.333A,
the RBI Fair Practices Code, FDCPA s.806–807, SEBI PFUTP, MAR 14–15, Basel III IRB. If you
implement a control, name the rule it implements.

**Never fake it.** If it cannot work here, make it say so.

---

## 17. Further reading

| Document | Covers |
|---|---|
| `docs/PROCESS.md` | A request end to end, node by node (557 lines — the deepest one) |
| `docs/ARCHITECTURE.md` | Components and data flow |
| `docs/AGENTS.md` | Each agent's purpose, tools and controls |
| `docs/CREDIT_AND_COLLECTIONS.md` | The lending models in detail |
| `docs/GUARDRAILS.md` | Every rail, the fail-closed rules, degradation |
| `docs/VALIDATION.md` | The conformance harness and all 28 scenarios |
| `docs/API.md` | Endpoint reference |
| `docs/SECURITY.md` | Authn/authz, RBAC, audit, secrets |
| `docs/DEPLOYMENT.md` | Compose, Helm, migrations, promotion on upgrade |
| `docs/OPERATIONS.md` | Runbooks, metrics, alerts |
| `docs/APPLICATIONS.md` | The two-app split and what lives where |
| `docs/BANKING_CONFIGURATION.md` | Sample data shape |
