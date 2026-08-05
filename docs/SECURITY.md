# Security

## Authentication

Three mechanisms, all first class:

1. **Password + optional TOTP MFA** — bcrypt with a SHA-256 pre-hash so passwords of any
   length are handled safely. Five failures lock the account for 15 minutes.
2. **API keys** — `fops_<prefix>_<secret>`, stored only as a SHA-256 hash and shown once.
   Per-key rate limits, expiry and revocation.
3. **OIDC SSO (Keycloak)** — authorisation-code exchange, users provisioned on first login
   with the `viewer` role until an administrator grants more.

Access tokens are short-lived JWTs; refresh tokens are persisted, single-use and revoked
on rotation, so a stolen refresh token stops working the moment the legitimate holder
refreshes.

## Authorisation

25 permissions across nine roles. Every endpoint declares the permission it needs; there
is no implicit access.

| Role | Purpose |
|---|---|
| `admin` | full administration |
| `platform_engineer` | build and operate agents, manage flags |
| `agent_builder` | create, version and publish agents and knowledge |
| `operator` | run agents, manage executions, invoke tools |
| `approver` | decide human-in-the-loop requests |
| `auditor` | read-only audit, security and cost |
| `analyst` | run agents against customer data |
| `viewer` | read-only dashboards |
| `service` | machine identity for API keys |

Segregation of duties is enforced at the approval endpoint: a requester cannot approve
their own request, and the decider must hold the role the request demands.

## Secrets

Resolution order: Vault (KV v2) → environment → encrypted database row. Database-stored
secrets are sealed with an HMAC-SHA256 keystream envelope authenticated by an HMAC tag —
tamper detection, not just obfuscation. Only masked hints are ever returned by the API.
The security dashboard flags a default `JWT_SECRET` and secrets past their rotation interval.

## Data protection

- **PII masking** — the guardrail node masks PAN, Aadhaar, card numbers, emails and SSNs
  before a response leaves the platform, per agent policy with an explicit allowlist.
- **Minimisation** — banking tools return masked account and card numbers; the Aadhaar
  tool returns only the last four digits and never stores the full number.
- **Authentication gate** — customer service tools refuse to read account data until
  `authenticate_customer` has succeeded in that execution.
- **Classification** — knowledge documents carry a classification that travels with
  retrieval results so the agent can flag confidential sources.
- **Retention** — traces, events, logs and cost records expire on schedule; regulatory
  artifacts follow the statutory schedule instead.

## Prompt-injection defence

Inputs are scanned for known override patterns and recorded against the execution when
found. Structural defences matter more: tools are allow-listed per agent, arguments are
schema-validated, data-writing tools are approval-gated, and the model never sees
credentials or holds a database handle. The worst case for a successful injection is a
tool call the agent was already permitted to make — and if that tool is gated, a human
still has to approve it.

## Audit

Every mutation is recorded with actor, actor type, action, resource, outcome, severity,
IP, user agent, request id and trace id. Authentication events, agent lifecycle changes,
configuration edits, publishes and rollbacks, approval decisions, secret writes, API key
creation and revocation, and user administration are all covered. The trail is queryable
by the `auditor` role and exportable.

## Network posture

- TLS terminates at the ingress; HSTS and a strict CSP are set by the web tier.
- A default-deny NetworkPolicy allows only intra-namespace traffic, DNS and outbound 443.
- Containers run as non-root with a read-only root filesystem, all capabilities dropped
  and the runtime default seccomp profile.
- CORS is restricted to the console origin.

## Reporting a vulnerability

Contact the platform security team. Do not open a public issue. Include reproduction
steps, affected version and observed impact.
