# Operations

## Health

| Endpoint | Meaning |
|---|---|
| `/health/live` | process is up — liveness probe |
| `/health/ready` | database reachable; reports cache, bus, storage, secrets and configured providers |
| `/health/startup` | startup probe |
| `/metrics` | Prometheus exposition |

`/health/ready` returns 200 with a warning (not 503) when no LLM provider is configured:
the platform is genuinely serving dashboards and tools, only agent execution is blocked.

## Alerts

Defined in `infra/prometheus/alerts.yml`.

| Alert | Condition | First action |
|---|---|---|
| `AgentErrorRateHigh` | agent error rate > 25% for 10m | inspect recent failed traces; pause the agent |
| `AgentLatencySlo` | p95 > 120 s for 15m | check provider latency and tool p95 |
| `LlmProviderErrors` | provider errors > 0.2/s | check the breaker; confirm the fallback chain has a model |
| `CircuitBreakerOpen` | any circuit open for 2m | identify the failing dependency |
| `DailyBudgetBurn` | 24h spend over budget | check cost by agent and model for a runaway loop |
| `QueueBacklog` | queue depth > 50 for 10m | scale the API; check for stuck executions |
| `ApiErrorRate` | 5xx rate > 5% for 5m | check logs and database health |
| `ApiDown` | target down for 2m | check pods and ingress |

## Incident runbook

**Severity** — Sev-1: customer-facing loss, confirmed breach, regulatory reporting failure.
Sev-2: major degradation or one agent failing for all users. Sev-3: partial with a
workaround. Sev-4: cosmetic. Any engineer may declare; only the incident commander closes
a Sev-1.

**Triage order**

1. `/health/ready` and `/api/v1/services` — what is actually down.
2. `/api/v1/platform-metrics` — circuit breaker states and latency percentiles.
3. Failed executions in the console; open the trace — node-level spans show the failing stage.
4. Isolate rather than escalate: pause the affected agent instead of taking the platform down.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"pause","reason":"INC-1234 elevated error rate"}' \
  https://finops.example.com/api/v1/agents/customer_service/lifecycle
```

**Common causes**

| Symptom | Usual cause | Fix |
|---|---|---|
| All executions fail with `provider_not_configured` | key missing, expired or rotated | update the secret; restart to reload |
| One agent fails, others fine | prompt or tool change | roll back the agent version |
| Latency doubled | provider degradation | check `finops_llm_latency_seconds` by provider; failover |
| Cost spike | tool-call loop | check `llm_call_count` on executions; lower `max_iterations` |
| Executions stuck `awaiting_approval` | no reviewer | check the approvals queue; the expiry job clears them after `APPROVAL_TIMEOUT_SECONDS` |
| `database is locked` (SQLite) | dev-only write contention | use PostgreSQL |

## Cost control

Review `/api/v1/costs/summary` daily. Attribution is available by agent, model, provider,
user, department, tool and category, with a run-rate forecast to month end.

Levers, in order of preference: lower `max_iterations`; route to a cheaper tier via the
agent's model setting; tighten `retrieval_top_k`; reduce `max_tokens`; lower
`PER_EXECUTION_COST_CAP_USD` to fail runaway executions fast.

## Approvals

Pending approvals hold a real execution open. Monitor the queue depth on the overview
page. Segregation of duties is enforced: the requester cannot decide their own request,
and the decider must hold the required role. Every decision, with reviewer and comments,
lands in the audit trail and on the approval timeline.

## Log investigation

Every log record carries `execution_id`, `correlation_id` and `trace_id`.

```bash
# everything for one execution
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/executions/$EXECUTION_ID/logs"

# errors across the platform in the last hour
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/logs?level=ERROR&since_minutes=60"

# export for offline analysis
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/logs/export?fmt=jsonl&since_minutes=1440" -o logs.jsonl
```

The console offers a live tail with level and agent filters.

## Scheduled jobs

| Job | Schedule | Purpose |
|---|---|---|
| `sync_knowledge` | every 2 hours | pull from configured connectors |
| `run_transaction_monitoring` | hourly | AML rules over the ledger |
| `expire_approvals` | every 15 minutes | expire stale requests and release executions |
| `apply_retention` | daily 03:30 | enforce the retention policy |
| `run_scheduled_agents` | every minute | fire cron-scheduled agent runs |

## Routine maintenance

- **Weekly** — review error rate and latency by agent; check evaluation scores for regression.
- **Monthly** — rotate API keys nearing expiry; review RBAC assignments; reconcile budgets.
- **Quarterly** — restore rehearsal; model catalogue price review; guardrail rule review.
- **On model release** — bench in the playground, update `DEFAULT_MODEL`, watch evaluation scores.
