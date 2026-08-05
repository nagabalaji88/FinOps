# API reference

Base path `/api/v1`. Interactive documentation at `/docs`; the machine-readable schema at
`/openapi.json`.

## Authentication

```bash
TOKEN=$(curl -fsS -X POST "$API/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@finops.local","password":"..."}' | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" "$API/api/v1/agents"
curl -H "X-API-Key: fops_ab12cd34_..." "$API/api/v1/agents"     # machine identity
```

MFA: when enabled, `/auth/login` returns 401 with `details.mfa_required` until `mfa_code`
is supplied.

## Errors

```json
{
  "error": {
    "code": "provider_not_configured",
    "message": "Provider 'openai' is not configured",
    "details": { "required": "OPENAI_API_KEY and base URL" }
  },
  "request_id": "9f2c…"
}
```

| Code | HTTP | Meaning |
|---|---|---|
| `unauthenticated` | 401 | missing or invalid credentials |
| `forbidden` | 403 | authenticated but lacking the permission |
| `not_found` | 404 | resource does not exist |
| `conflict` | 409 | duplicate resource |
| `validation_error` | 422 | invalid input or state |
| `rate_limited` | 429 | token bucket exhausted; `details.retry_after_seconds` |
| `budget_exceeded` | 402 | execution cost cap reached |
| `provider_not_configured` | 503 | a required provider is not set up |
| `circuit_open` | 503 | dependency is failing and isolated |

## Endpoints

### Agents
| Method | Path | Permission |
|---|---|---|
| GET | `/agents` | `agent:read` |
| GET | `/agents/{key}` | `agent:read` |
| GET | `/agents/graph` | `agent:read` |
| GET | `/agents/roadmap` | `agent:read` |
| POST | `/agents` | `agent:write` |
| POST | `/agents/{key}/execute` | `agent:execute` |
| POST | `/agents/{key}/lifecycle` | `agent:lifecycle` |
| PUT | `/agents/{key}/config` | `agent:write` |
| GET | `/agents/{key}/versions` | `agent:read` |
| POST | `/agents/{key}/versions/{n}/publish` | `agent:publish` |
| POST | `/agents/{key}/versions/{n}/rollback` | `agent:publish` |

### Executions
| Method | Path | Purpose |
|---|---|---|
| GET | `/executions` | filter by agent, status, user, window |
| GET | `/executions/{id}` | full record including input, output, plan, reasoning |
| GET | `/executions/{id}/trace` | spans with offsets and widths for the waterfall |
| GET | `/executions/{id}/graph` | React Flow DAG annotated with what ran |
| GET | `/executions/{id}/events` | event log, `?after=` for incremental fetch |
| GET | `/executions/{id}/logs` | correlated log records |
| GET | `/executions/{id}/stream` | SSE: replay then follow |
| WS | `/executions/{id}/ws` | WebSocket equivalent |
| POST | `/executions/{id}/cancel` | cancel a running execution |

### Approvals, cost, knowledge, tools, security
`/approvals`, `/approvals/{id}/decision`, `/approvals/stream/live` ·
`/costs/summary`, `/costs/records` ·
`/knowledge/sources`, `/knowledge/sources/{key}/sync`,
`/knowledge/sources/{key}/documents`, `/knowledge/search`, `/knowledge/documents` ·
`/tools`, `/tools/{name}/invoke` ·
`/services`, `/platform-metrics`, `/platform-metrics/tools`, `/platform-metrics/evaluations` ·
`/security/overview`, `/security/audit`, `/security/secrets`, `/security/feature-flags/{key}` ·
`/playground/run`, `/playground/models`, `/playground/estimate` ·
`/evaluations`, `/evaluations/{execution_id}/run`, `/evaluations/{execution_id}/feedback` ·
`/dashboard/overview`, `/dashboard/ai-usage`, `/dashboard/timeseries`, `/dashboard/knowledge`

## Executing an agent

```bash
curl -X POST "$API/api/v1/agents/knowledge_assistant/execute" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"input":{"query":"What is our SAR filing deadline?"},"thread_id":"conv-42"}'
```

```json
{
  "execution_id": "8f2c…",
  "status": "queued",
  "trace_id": "63bc…",
  "correlation_id": "f3e9…",
  "stream_url": "/api/v1/executions/8f2c…/stream"
}
```

Pass `"wait": true` to block until completion — useful for scripts, not for the UI.

## Streaming protocol

Server-Sent Events. Persisted events replay first (so a late subscriber loses nothing),
then live events follow. `?after=<sequence>` resumes from a known point. A `heartbeat`
event every 15 s keeps intermediaries from closing the connection.

```
event: llm.completed
id: 7
data: {"sequence":7,"type":"llm.completed","node":"llm","timestamp":"…","payload":{…}}
```

Event types: `execution.started`, `node.started`, `node.completed`, `node.skipped`,
`node.failed`, `planning`, `reasoning`, `knowledge.search`, `vector.search`,
`memory.retrieval`, `database.query`, `api.call`, `llm.started`, `llm.completed`,
`tool.started`, `tool.completed`, `tool.failed`, `retry`, `guardrail`, `validation`,
`approval.requested`, `approval.decided`, `final.response`, `execution.completed`,
`execution.failed`, `execution.cancelled`, `execution.suspended`.

The stream closes on any terminal event.

## Approval flow

```bash
# 1. execution suspends -> status "awaiting_approval"
curl -H "Authorization: Bearer $TOKEN" "$API/api/v1/approvals?status=pending"

# 2. a reviewer decides (requires the approval:decide permission)
curl -X POST "$API/api/v1/approvals/$APPROVAL_ID/decision" \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"decision":"approve","comments":"Confirmed with the customer"}'

# 3. the execution resumes from its checkpoint automatically
```

Rejecting returns "Rejected by human reviewer" to the model as the tool result, so the
agent can respond appropriately rather than failing.

## Rate limits

Default 240 requests per minute per principal; API keys carry their own limit. Exceeding
returns 429 with `details.retry_after_seconds`.
