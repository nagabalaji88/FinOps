# Architecture

## 1. Shape of the system

The platform is a control plane, not a chat wrapper. Agents are declarative
specifications executed by one shared, deterministic graph. That means every agent gets
the same tracing, budget enforcement, guardrails, approval semantics and audit — nobody
re-implements them per agent, and nobody can skip them.

```
 ┌──────────────┐   SSE / WebSocket    ┌──────────────┐
 │ React console│ ◀──────────────────▶ │   FastAPI    │
 └──────────────┘                      └──────┬───────┘
                                              │
                                     ┌────────▼─────────┐
                                     │ Execution engine │
                                     └────────┬─────────┘
        ┌───────────────┬──────────────┬──────┴───────┬─────────────────┐
        ▼               ▼              ▼              ▼                 ▼
  Model router     Tool registry   RAG pipeline   Approval gate     Event bus
   9 providers     40 tools        hybrid search  suspend/resume    SSE · Redis · Kafka
```

## 2. Execution graph

`app/engine/nodes.py` defines the node set; `app/engine/executor.py` runs it.

| Node | Responsibility | Skipped when |
|---|---|---|
| `planner` | LLM produces a JSON plan: objective, steps, required tools, risk level | never |
| `retriever` | Hybrid vector + BM25 retrieval over the agent's knowledge sources | agent has no sources, or the plan sets `needs_knowledge_search: false` |
| `memory` | Loads the conversation thread and durable facts | memory disabled or no thread id |
| `llm` | The agentic loop: model decides, tools execute, observations feed back | never |
| `tools` | Executes tool calls (inside the `llm` node) | no tool calls requested |
| `validation` | Non-empty response, tool success, citation presence, output schema, groundedness | never |
| `guardrails` | PII masking, blocked terms, prompt-injection detection, mandated disclaimers | never |
| `human_approval` | Suspends for a reviewer when policy requires | agent does not require final approval, or risk below threshold |
| `response` | Finalises output, citations, artifacts and memory | never |

Each node opens a span, emits streaming events, and commits its work before the next node
runs. Progressive commits mean the console sees the trace build in real time and write
transactions stay short.

### The reasoning loop

The `llm` node loops until the model returns a final answer or `max_iterations` is hit:

1. Call the model with the tool schemas the agent is allowed to use.
2. If the response has no tool calls → that is the final answer, exit.
3. Otherwise execute each tool call:
   - unknown tool → return a structured error to the model rather than crashing;
   - tool marked `requires_approval` and not yet approved → raise `ApprovalPause`;
   - previously rejected → return "Rejected by human reviewer" to the model;
   - otherwise invoke it, record a span, emit events, append the result as a `tool` message.
4. Loop.

Budget is checked at the top of every iteration; exceeding the per-execution cap raises
`BudgetExceededError` and the execution fails cleanly with everything recorded.

### Suspend and resume

`ApprovalPause` is not an error path bolted on — it is how human-in-the-loop works:

1. The engine serialises `ExecutionState` to `executions.checkpoint`: messages, plan,
   retrieved context, tool results, pending tool calls, budget, span ids, sequence number.
2. Status becomes `awaiting_approval`; the root span stays open; an `Approval` row is
   created and broadcast.
3. A reviewer decides. `resume_after_approval` deep-copies the checkpoint, records the
   decision against the tool-call id, marks the node to resume at, and flags the JSON
   column as modified so SQLAlchemy persists it.
4. A fresh task restores the state and re-enters the reasoning node. Pending tool calls
   replay; tool calls already answered are skipped by matching `tool_call_id` against the
   restored message history.

The deep copy and `flag_modified` matter: mutating a JSON column in place is invisible to
the ORM, which silently loses the resume point.

## 3. Model router

`app/llm/router.py` selects a model per call:

1. explicit model id, if its provider is configured;
2. the agent's preferred model;
3. policy selection — required capability (tools, vision), tier, cost ceiling;
4. cheapest available candidate.

Every call goes through a per-provider circuit breaker and a retry policy with exponential
backoff and jitter. If a provider fails, the router walks a fallback chain of same-tier
models from other configured providers and logs that a fallback was used. Provider misconfiguration
is never retried — it fails immediately with the settings needed.

Metering happens on every response: tokens (input, output, cached), computed cost from the
model catalogue, latency, provider and model, all pushed to Prometheus and the cost ledger.
Cost records join the caller's transaction through an ambient-session context variable, so
metering is part of the same unit of work as the execution it belongs to.

### Provider implementations

- **OpenAI-compatible** — one class serves OpenAI, Azure OpenAI (deployment-style URLs and
  `api-key` header), Mistral, DeepSeek, Together and Ollama.
- **Anthropic** — Messages API, tool use, SSE streaming with incremental `input_json_delta`
  accumulation for tool arguments.
- **Google** — `generateContent` with function declarations and `batchEmbedContents`.
- **Bedrock** — the Converse API with AWS Signature Version 4 implemented directly against
  the specification, so boto3 is not a hard dependency.

## 4. Tools

A tool is a Pydantic-validated async function registered with metadata: category, timeout,
retry count, idempotency, whether it writes data, and whether it needs human approval.

The registry wraps every invocation with argument validation, a timeout, a circuit
breaker, retry (only for idempotent tools), latency measurement, health tracking
(p50/p95 over a rolling window) and structured error capture. A tool never raises into the
engine — it returns a `ToolResult` the model can reason about.

Tools receive a `ToolContext` carrying the execution id, agent key, caller identity, roles
and the active database session, plus a scratch dict scoped to the execution. The customer
service tools use that scratch space to enforce that authentication happened before any
account data is read, within this execution only.

## 5. Retrieval

Ingestion: structure-aware chunking (headings preserved, sentence boundaries respected,
configurable overlap) → embedding → storage. Chunks keep their vector as a float32 buffer
alongside the text, and are mirrored into Qdrant when it is configured.

Query: embed → vector search → keyword candidate pool → BM25 scoring (k1=1.5, b=0.75) →
combined score `0.65 × vector + 0.35 × normalised BM25` → threshold → top-k. Both component
scores are returned so retrieval quality is debuggable.

Embeddings come from the configured provider. Without one, a local feature-hashing
vectoriser (word unigrams + bigrams, sublinear TF, L2 normalised, 768 dimensions) keeps
retrieval working deterministically at lower semantic quality. The method in use is
reported on every search response and stored on every chunk — it is never presented as a
provider embedding.

## 6. State and durability

- `executions` — one row per run with status, timings, tokens, cost, node path, checkpoint
- `spans` — the trace: parent/child, kind, status, timings, payloads, tokens, cost, retries
- `execution_events` — append-only sequence for stream replay from any offset
- `log_records` — structured logs correlated by execution, correlation and trace id
- `cost_records` — the ledger, sliced by agent, model, provider, user, department, tool
- `approvals` — request, payload under review, risk, reviewer, comments, timeline

A late subscriber replays persisted events, then follows the live bus from the last
sequence number it saw — no gaps, no duplicates.

## 7. Failure handling

| Mechanism | Where |
|---|---|
| Retry with exponential backoff and jitter | LLM calls, tool calls, embeddings |
| Circuit breaker (closed → open → half-open) | per provider, per tool |
| Bulkhead | caps concurrent executions |
| Timeout | per tool and per execution |
| Budget cap | per execution, enforced each loop iteration |
| Rate limiting | token bucket per principal or API key |
| Graceful degradation | every infrastructure adapter has an embedded fallback |

## 8. Multi-pod behaviour

The API and engine are stateless. Live streaming fans out through Redis pub/sub, so any
pod can serve a subscriber for an execution running on another pod. Kafka receives a
durable copy of domain events when configured. Scheduled and long-running work goes to
Celery workers. Without Redis the bus is in-process, which is correct for a single pod and
is reported as such on the services dashboard.
