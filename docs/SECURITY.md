# Security

## Authentication

Three mechanisms, all first class:

1. **Password + optional TOTP MFA** — bcrypt with a SHA-256 pre-hash so passwords of any
   length are handled safely. Five failures lock the account for 15 minutes; the counter and
   the failure audit row are committed in their own transaction, because the rejection
   rolls the request's transaction back and would otherwise take the increment with it.
   Failed attempts are additionally throttled per source and account
   (`LOGIN_FAILURE_LIMIT_PER_MINUTE`); a correct password never spends that budget, so
   throttling password guessing cannot be turned into a way to lock people out.
   Changing the second factor requires the current password, plus the factor already in
   force, and revokes every live session. The candidate secret travels in a signed,
   short-lived ticket rather than being written to the user row, so an abandoned enrolment
   cannot strand an account whose working factor was overwritten.
2. **API keys** — `fops_<prefix>_<secret>`, stored only as a SHA-256 hash and shown once.
   Per-key rate limits, expiry and revocation. A key's scopes are **intersected** with its
   owner's role permissions, so a key minted for one integration cannot do everything its
   owner can; an empty scope list means the key inherits the owner. Unknown scopes are
   refused at creation rather than silently ignored.
3. **OIDC SSO (Keycloak)** — the browser does not build its own authorisation URL.
   `POST /auth/sso/authorize` mints a one-time `state`, a `nonce` and a PKCE verifier that
   stays server-side; the callback requires the `state`, consumes it before exchanging,
   checks the redirect URI against the one recorded, and validates the ID token's
   signature, issuer, audience and nonce. An account is created or linked only when the
   provider reports the email as verified, and a subject that does not match an already
   linked account is refused. Users are provisioned with the `viewer` role until an
   administrator grants more.

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
secrets are sealed with AES-256-GCM under a key derived from `SECRET_ENCRYPTION_KEY` by
HKDF. That key is deliberately separate from `JWT_SECRET`: sharing them means one weak
value compromises both the tokens and every stored provider credential, and rotating the
signing key would leave the stored credentials undecryptable. Values written by earlier
releases carry no version marker and are still readable, but nothing writes that format
any more. Only masked hints are ever returned by the API. The security dashboard flags a
default `JWT_SECRET` and secrets past their rotation interval.

A production deployment refuses to start while `JWT_SECRET` or `SECRET_ENCRYPTION_KEY` is
absent or at its shipped default, while `BOOTSTRAP_ADMIN_PASSWORD` is the shipped default,
while `TRUSTED_HOSTS`, `CORS_ORIGINS` or `FORWARDED_ALLOW_IPS` contains a wildcard, or
while `SEED_DEMO_USERS` is on — the demo identities all share the bootstrap password.
Outside production the same list is logged as a warning at startup.

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

- `TRUSTED_HOSTS` lists the host headers the API answers to, and `FORWARDED_ALLOW_IPS` the
  peers whose `X-Forwarded-For` it believes. Neither may be a wildcard in production: the
  first lets an attacker mint absolute URLs pointing at a host they control, the second
  lets any caller forge the client IP that rate limiting throttles on and the audit trail
  records.
- `/docs`, `/redoc` and `/openapi.json` are withheld in production unless
  `EXPOSE_API_DOCS=true`; the schema enumerates every route for an unauthenticated caller.
  `/metrics` is governed by `EXPOSE_METRICS_ENDPOINT` and is left on in the shipped
  manifests because the ServiceMonitor scrapes the pod directly — the ingress routes only
  `/api`, `/health` and `/`, so neither path is reachable from outside the cluster.
- Readiness fails rather than degrading quietly when artifact storage has fallen back to
  local disk in an environment that requires durable storage: the pod mounts an
  `emptyDir`, so the fallback loses every artifact on restart and shares none between
  replicas.
- TLS terminates at the ingress; HSTS and a strict CSP are set by the web tier.
- A default-deny NetworkPolicy allows only intra-namespace traffic, DNS and outbound 443.
- Containers run as non-root with a read-only root filesystem, all capabilities dropped
  and the runtime default seccomp profile.
- CORS is restricted to the console origin.

## Reporting a vulnerability

Contact the platform security team. Do not open a public issue. Include reproduction
steps, affected version and observed impact.
