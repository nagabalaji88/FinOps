# Two applications

The front end is two separately built and separately deployed applications sharing one
backend, one database and one auth realm.

```
                 ┌─────────────────────┐        ┌──────────────────────────┐
                 │  Execute (app 1)    │        │  Platform console (app 2)│
                 │  login              │        │  login                   │
                 │  Execute            │        │  overview, agents,       │
                 │  Analytics          │        │  executions, approvals,  │
                 │                     │        │  cost, knowledge, tools, │
                 │                     │        │  services, evaluation,   │
                 │                     │        │  playground, builder,    │
                 │                     │        │  logs, security,         │
                 │                     │        │  geography, settings     │
                 └──────────┬──────────┘        └────────────┬─────────────┘
                            │      @finops/shared            │
                            │  design system · API client    │
                            │  auth / UI / toast stores      │
                            └───────────┬────────────────────┘
                                        ▼
                        FastAPI · execution engine · PostgreSQL
```

## Why the split is at the front end

The two audiences want different things from the same platform. An operator running KYC
or AML cases needs one screen that executes and one that says whether execution is healthy.
An engineer or a compliance officer needs the trace waterfall, the cost ledger, the
knowledge corpus, the audit trail and the agent builder.

Splitting only the deployment keeps one system of record. A run started in Execute is the
same `Execution` row the console traces, prices and audits — same id, same trace, same
approvals, same cost records. Two backends would have meant duplicating the ledger or
synchronising it, and the console would no longer see what Execute did.

## Layout

```
apps/execute/       Application 1
apps/console/       Application 2
packages/shared/    @finops/shared — everything both applications must agree on
```

`@finops/shared` holds the typed API client (including the SSE subscription), the design
system, the auth / UI / toast stores, the formatting helpers, the Tailwind theme preset and
the sign-in screen. It is the single definition of both the wire contract and the visual
language, so the two applications cannot drift apart.

npm workspaces links the package; Vite and TypeScript both resolve `@finops/shared`
straight to its TypeScript source, so there is no build step between editing shared code
and seeing it in either application.

```bash
npm install             # once, at the repository root
npm run dev:execute     # http://localhost:5174
npm run dev:console     # http://localhost:5173
npm run typecheck       # shared + both applications
npm run test
npm run build           # both dist/ bundles
```

## Application 1 — Execute

Two screens behind a login.

### Execute

Two agents are on the board at a time. All five implemented agents stay registered and
executable on the backend; the board decides which pair is in front of the operator. The
default pair comes from `VITE_EXECUTE_AGENTS` (default
`customer_service,aml_investigation`); either slot can be swapped for another implemented
agent from the card menu, and the choice is remembered per browser — including across
tabs, which follow each other through the `storage` event.

The run console is the complete execution path, not a launcher:

| Step | What happens |
|---|---|
| Input | The form is generated from the agent's declared `input_schema`, seeded with its `example_input`, so it always matches what the backend accepts |
| Submit | `POST /agents/{key}/execute` with `trigger: manual` — the same endpoint everything else uses |
| Live | The SSE stream replays anything already recorded and then follows: node transitions, retrieval, model calls with tokens and cost, tool calls with their arguments and results, guardrail and validation findings |
| Gate | When the engine suspends on a human approval, the gate renders inline with the payload being approved. Approve or reject with a comment and the execution resumes from its checkpoint |
| Segregation of duties | If the signed-in user started the run, or lacks the required role, the gate says who must decide and waits for them rather than offering buttons the API would reject |
| Cancel | `POST /executions/{id}/cancel` while a run is in flight |
| Result | Final response, citations, structured output, and the durable figures — latency, tool calls, model calls, tokens, cost — read back from the execution record |

The stream closes when the engine suspends for approval, so the console tracks the last
sequence number it saw and re-subscribes from there once a decision is recorded. The run
continues in the same panel with an unbroken event feed.

Below the console, the last runs for the selected agent, with status, latency, tool count
and any error.

### Analytics

Throughput (succeeded against failed over 24h / 72h / 7d), success rate, average latency
and spend against budget; spend by model; a per-agent breakdown for the two agents on the
board; and tool reliability measured from every invocation those agents have made.

Every figure comes from the platform's own telemetry — the execution ledger, the cost
records, the per-tool health counters, the approval log. Nothing on this screen is
estimated.

## Application 2 — Platform console

Unchanged from the single-application build: overview, agent catalogue and detail,
executions with the trace waterfall and React Flow DAG, approvals, cost, knowledge, tools
and APIs, connected services, evaluation, prompt playground, agent builder, logs, security,
geography and settings.

## Deployment

Two images, two Deployments, two Services, routed by host.

```bash
docker compose up -d --build
# Execute          http://localhost:8080
# Platform console http://localhost:8090
```

```yaml
# infra/helm/finops/values.yaml
web:
  execute:
    enabled: true
    host: execute.finops.example.com
    image: { repository: ghcr.io/finops/agent-execute }
  console:
    enabled: true
    host: console.finops.example.com
    image: { repository: ghcr.io/finops/agent-console }
```

Either application can be scaled, upgraded or disabled without touching the other. Set
`CORS_ORIGINS` to both hostnames — the API is shared, so it must accept both origins.

Both images are built from the repository root, because each application shares a workspace
with `@finops/shared`:

```bash
docker build -f apps/execute/Dockerfile -t agent-execute .
docker build -f apps/console/Dockerfile -t agent-console .
```

## Access control

There is one identity and one RBAC matrix. Execute needs `agent:read`, `execution:read`,
`execution:write`, `approval:read`, `approval:decide`, `metric:read`, `cost:read` and
`tool:read`. A user without `approval:decide` can still run agents; they simply cannot
clear a gate, and the console says so instead of failing on submit. Nothing about the
split grants permission the API would not already grant — the applications are two views
of one authorisation model, not two trust boundaries.
