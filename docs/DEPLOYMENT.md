# Deployment

## Prerequisites

| Target | Requirements |
|---|---|
| Local | Python 3.11–3.13, Node 22+ |
| Compose | Docker 24+, 8 GB RAM free |
| Kubernetes | 1.28+, ingress-nginx, cert-manager, External Secrets Operator, Prometheus Operator |

At least one LLM provider key is required for agent execution. Everything else
(dashboards, retrieval, screening, monitoring, RBAC, audit) works without one.

## 1. Docker Compose

```bash
cp .env.example .env        # set JWT_SECRET, BOOTSTRAP_ADMIN_PASSWORD and a provider key
docker compose up -d --build
docker compose logs -f api
```

`migrate` runs `alembic upgrade head` then `python -m app.cli init-db` and exits; `api`
waits for it to complete successfully.

`init-db` is written to be run on every upgrade, not only on an empty database. An agent
that has shipped since this database was seeded still carries its roadmap row —
`coming_soon`, `disabled`, no configuration — and would keep answering *"Agent 'x' is not
implemented yet"* for a released agent. Seeding promotes those rows, logs
`agent_promoted`, and reports the count as `agents_updated`. It promotes `coming_soon` only:
an agent an operator deliberately disabled stays disabled.

Load the sample bank when you want data to work against:

```bash
docker compose exec api python -m app.cli seed-banking --customers 24
docker compose exec api python -m app.cli load-sanctions      # OFAC SDN, public data
```

Verify:

```bash
curl -fsS localhost:8000/health/ready | jq
docker compose exec api python -m app.cli health
```

## 2. Kubernetes with Helm

```bash
helm dependency update infra/helm/finops

helm upgrade --install finops infra/helm/finops \
  --namespace finops --create-namespace \
  --set image.api.tag=1.0.0 \
  --set image.web.tag=1.0.0 \
  --set ingress.host=finops.yourbank.example \
  --set config.corsOrigins=https://execute.yourbank.example\,https://console.yourbank.example \
  --set externalSecrets.vaultPathPrefix=finops \
  --wait --timeout 10m
```

Secrets come from Vault through the External Secrets Operator. Store them once:

```bash
vault kv put secret/finops/runtime \
  DATABASE_URL="postgresql+asyncpg://finops:...@finops-postgresql:5432/finops" \
  JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  BOOTSTRAP_ADMIN_PASSWORD='...' \
  ANTHROPIC_API_KEY='...' \
  OPENAI_API_KEY='...'
```

For clusters without ESO, set `externalSecrets.enabled=false` and `secrets.create=true`
with `secrets.data` — acceptable for non-production only.

Raw manifests are in `infra/k8s/` if you prefer kustomize or ArgoCD.

## 3. Migrations

Forward-only and backwards compatible for one release, so a rolling deploy is safe.

```bash
alembic upgrade head              # applied automatically by the init container
alembic revision --autogenerate -m "description"
alembic downgrade -1              # local only
```

CI verifies `upgrade head → downgrade base → upgrade head` against PostgreSQL on every run.

## 4. Scaling

| Component | Scaling signal | Notes |
|---|---|---|
| API | CPU 70%, memory 75%, queue depth > 10/pod | stateless; HPA 3→20 |
| Workers | Celery queue depth | stateless |
| Beat | fixed at 1 | `Recreate` strategy, never scale up |
| PostgreSQL | vertical, then read replicas | primary takes all writes |
| Qdrant | shard by collection | optional; database fallback is exact but slower |

`MAX_CONCURRENT_EXECUTIONS` bounds in-flight executions per pod (bulkhead). Effective
platform concurrency is that value × replica count. Raise it only after confirming
provider rate limits and database connection headroom.

Sizing guidance: an API pod at 2 vCPU / 3 GiB sustains roughly 40–60 concurrent
executions dominated by provider latency. Budget one worker replica per 500 scheduled
jobs per hour.

## 5. Backup and restore

RPO 15 minutes, RTO 1 hour for the API tier and 4 hours for the whole platform.

- **PostgreSQL** — continuous WAL archiving plus a nightly base backup, 35-day retention.
- **Object store** — versioned buckets with cross-region replication; regulatory artifacts
  (KYC reports, SARs) retained 7 years and exempt from routine purge.
- **Vector store** — rebuildable with `finops reindex-knowledge`; weekly snapshot shortens recovery.
- **Vault** — snapshots every 6 hours, encrypted, 90-day retention.

Restore:

```bash
# 1. restore the base backup and replay WAL to the target point in time
# 2. restore the object store from the replicated bucket
kubectl exec deploy/finops-api -- python -m app.cli reindex-knowledge
kubectl exec deploy/finops-api -- python -m app.cli health
# 3. smoke: sign in, execute an agent, search knowledge, decide an approval
```

Rehearse quarterly in an isolated environment and keep the evidence for audit.

## 6. Data retention

`DATA_RETENTION_DAYS` (default 400) governs spans, events, logs and cost records. The
nightly `finops-retention` CronJob applies it. Preview before enabling:

```bash
python -m app.cli retention            # dry run, reports counts
python -m app.cli retention --apply
```

## 7. Zero-downtime releases

1. Deploy the new image with `maxUnavailable: 0`; the readiness probe gates traffic.
2. `preStop` sleeps 10 s so in-flight requests drain before shutdown.
3. Watch the error-rate and latency alerts for 15 minutes.
4. Roll back with `helm rollback finops` — the previous image and config return together.

Agent configuration is versioned separately from the deployment. A bad prompt change is
reverted with `POST /api/v1/agents/{key}/versions/{n}/rollback` without a redeploy.

## 8. Post-install checklist

- [ ] `JWT_SECRET` rotated away from the default (the security dashboard flags this)
- [ ] `BOOTSTRAP_ADMIN_PASSWORD` changed and the seeded demo accounts removed or disabled
- [ ] At least one provider key configured and visible on Connected Services
- [ ] MFA enforced for every human account with approval rights
- [ ] Budgets set to real figures for the business unit
- [ ] Prometheus scraping `/metrics`; Grafana dashboard imported; alert routes tested
- [ ] Backup job verified by an actual restore, not just a green tick
- [ ] `CORS_ORIGINS` restricted to the two console hostnames — comma-separated, no wildcard
