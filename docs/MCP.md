# Model Context Protocol

MCP is how the ecosystem ships tools now — it was donated to the Linux Foundation's AI
Agent Infrastructure Foundation in December 2025, and the servers worth having are published
for it rather than as bespoke integrations. Consuming it means an agent here can use a
vendor's tool without anyone writing an adapter for it.

The design decision that matters is what it is **not**: a second tool path.

Tools are the only way an agent reaches a system of record, and everything that makes that
safe lives in the tool registry — the human-approval gate, the permission check, the circuit
breaker, the retry policy, the timing, the metrics, the audit trail. A parallel MCP path
would have to reimplement all of it, and would get some of it wrong.

So an MCP server's tools are adapted into ordinary `Tool` objects and registered in the same
registry. An external tool is reached through the same `registry.invoke` as a built-in one.
An operator reading an execution trace cannot tell that those controls had to be arranged —
because they didn't.

```
MCP server ──JSON-RPC/HTTP──> MCPClient ──> Tool(name="mcp.<server>.<tool>")
                                                 │
                                      registry.invoke ── the one path
                                                 │
            approval gate · permission · breaker · retry · metrics · audit
```

## Configuration

Off by default. Adopting someone else's tool is an explicit act.

```bash
MCP_ENABLED=true
MCP_SERVERS='[
  {
    "name": "market-data",
    "url": "https://mcp.vendor.example/rpc",
    "token_env": "MARKET_DATA_MCP_TOKEN",
    "allowed_tools": ["get_quote", "get_curve"],
    "read_only_tools": ["get_quote", "get_curve"],
    "timeout_seconds": 20
  }
]'
```

| Field | Meaning |
|---|---|
| `name` | Namespace for the tools, and the identifier in logs, traces and approval payloads. Must match `^[a-z0-9][a-z0-9_-]{0,38}$`. |
| `url` | HTTPS, or `http://127.0.0.1` for local development. Plaintext to anywhere else is refused. |
| `token_env` | The **name** of the environment variable holding the bearer token. Never the token itself. |
| `allowed_tools` | When set, only these are adopted. A server that starts advertising a new tool cannot thereby hand an agent new reach. |
| `read_only_tools` | Tools the operator has read and confirmed only read. Everything else is gated. |
| `timeout_seconds` | Per-call timeout, enforced by the registry like any other tool's. |

Servers are read from settings — never from a model, a request, or a database row an agent
can write. A configuration that does not parse is logged and ignored rather than raised: a
typo in a config blob must not stop the service starting.

Adoption puts a tool in the catalogue; it does not put it in front of an agent. An agent
sees only the tools named in its own registry entry, so reaching a newly adopted tool takes
a second, deliberate step: adding `mcp.<server>.<tool>` to that agent's `tools`. Enabling a
server does not silently widen what any existing agent can do.

## The strict defaults, and why

**Every external tool requires a human decision.** `REVIEW_EXEMPT_WRITES` exempts built-in
writes that were audited one at a time, each with its reason recorded. Nothing about a
third-party tool is audited, so the assumption is that it writes. Being wrong in that
direction costs a reviewer one click; being wrong in the other costs a write nobody
approved. `read_only_tools` is the operator saying they have read it.

**A gated external tool is never retried.** `idempotent` is false unless the tool is
declared read-only, because retrying someone else's write is how one payment becomes two.

**Names are namespaced** `mcp.<server>.<tool>`. A server cannot advertise
`record_credit_decision` and have an agent reach it believing it is ours. Namespacing makes
the collision harmless; adoption is refused anyway, for the whole server, because a server
reaching for our names is either confused or hostile and an operator should hear about it.

**A description is untrusted input on the most sensitive path there is.** It goes into the
model's prompt, outside what the rails screen. Control characters are stripped, the prompt
framing our own instructions use (` ``` `, `<|`, `|>`) is defanged, the text is capped at
1 KiB, and the result is prefixed `[MCP · <server>]` so the model and anyone reading a trace
see it as third-party text.

**A new permission, not an existing one.** External invocation requires
`tool:invoke_external`, which is **not** in `OPERATOR`. Being allowed to run our tools is a
different decision from being allowed to run someone else's; `admin` and
`platform_engineer` carry it, and an organisation grants it further where it means to.

**It documents itself.** Because adopted tools are ordinary registry entries, the generated
Annex IV technical documentation (`python -m app.cli annex-iv`) lists every third-party tool
in the same inventory as the built-ins, with the gate each one is subject to. A vendor tool
cannot be in use and absent from the compliance record.

### `required_permission` is now enforced

`Tool.required_permission` existed, was declared, and was read by nothing — documentation
wearing the costume of a control. It is now checked in `ToolRegistry.invoke`, the one place
both the engine's tool node and the direct-invocation endpoint go through, so a capability
cannot be reached through one path because the other was the one that checked. No built-in
tool declares a permission, so the change is inert for them and real for MCP tools.

## What the client implements, and what it refuses to

Three methods — `initialize`, `tools/list`, `tools/call` — over Streamable HTTP, with both
`application/json` and `text/event-stream` replies handled, session-id propagation, and
cursor pagination bounded so a server that paginates forever cannot hang a boot.

It declares **no** client capabilities. A server that saw `sampling` advertised would be
entitled to ask this platform to run a model on its behalf, and that is a capability nobody
asked for and nobody gated. Resources and prompts are not implemented for the same reason:
the surface exposed to an agent is tools.

A tool failure reported inside a `200` (`isError: true`) is raised as an error rather than
returned as data — otherwise the model is handed the error text as though it were the
answer.

It is written against the wire protocol rather than an SDK. The dependency closure of this
service is audited on every build, and a transport this small does not justify adding to it.
The protocol revision (`2025-06-18`) is pinned, and a server negotiating a different one is
logged rather than assumed compatible.

## Failure is not fatal

A third-party server being down does not stop this platform from starting. Each server's
outcome is recorded in a summary that `/health/ready` reports:

```json
{"checks": {"mcp": "2 external tools"},
 "warnings": ["MCP server(s) could not be reached, so their tools are not in the catalogue: market-data"]}
```

The deployment stays ready. An agent that needed an absent tool gets an unknown-tool result
for that call, which is a visible, local failure rather than a global one.

## Tests

`backend/tests/test_mcp.py`, 36 tests against a fake server, no network:

- the protocol — initialize, list, call, SSE replies, session id, bearer token, pagination
  to the end, an endless cursor that returns anyway, an unreachable host as an `MCPError`,
  and `isError` inside a 200 treated as a failure;
- descriptions as untrusted input — prompt framing defanged, length capped, control
  characters stripped, an empty description attributed to its server;
- the controls — an external tool is gated by default, a read-only one is not, a gated one
  is never retried, the external permission is demanded, a server advertising a built-in
  name is refused outright, a server that is down does not stop startup, shutdown closes
  the clients it opened, and an adopted tool really does run through `registry.invoke`
  **and is refused for a role without the grant**;
- arguments — the server's schema becomes a validating model, a schema-less tool passes
  arguments through, and a schema type we cannot map is accepted rather than rejected;
- configuration — plaintext URLs refused, loopback allowed, unsafe server names refused,
  tokens named rather than embedded, a missing token variable logged rather than dropping
  the server, and the allow-list honoured.
