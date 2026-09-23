"""Application configuration.

Every external dependency is optional at runtime: when its connection settings are
absent the corresponding adapter falls back to an embedded implementation and the
service is reported as ``not_configured`` on the Connected Services dashboard.
Nothing is faked -- a capability that requires an unconfigured provider fails loudly.
"""

from __future__ import annotations

import functools
import os
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The test suite isolates itself by clearing provider credentials out of os.environ, which a
# dotenv file silently defeats: it is read straight off disk, so nothing removed from the
# environment removes it. A developer with a working .env would then run a suite that reaches
# their real providers. Setting this makes that isolation total.
_IGNORE_DOTENV = os.getenv("FINOPS_IGNORE_DOTENV", "").lower() in {"1", "true", "yes"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=() if _IGNORE_DOTENV else (".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ---------------------------------------------------------------
    app_name: str = "FinOps AI Command Center"
    environment: Literal["local", "dev", "staging", "production"] = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"
    # NoDecode is required: pydantic-settings JSON-decodes complex types straight from the
    # environment *before* any validator runs, so without it a comma-separated value —
    # the form every deployment writes — fails at import with an opaque SettingsError.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",  # platform console (dev)
            "http://localhost:5174",  # execute console (dev)
            "http://localhost:4173",
            "http://localhost:4174",
        ]
    )
    root_path: str = ""
    #: Host headers this deployment answers to. A wildcard lets an attacker mint absolute
    #: URLs (password-reset links, redirects) pointing at a host they control, so
    #: production refuses to start on one -- see :meth:`production_misconfigurations`.
    trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    #: Peers whose ``X-Forwarded-For``/``X-Forwarded-Proto`` this deployment believes.
    #: Read by uvicorn itself from ``FORWARDED_ALLOW_IPS``; mirrored here so that
    #: :meth:`production_misconfigurations` can refuse a wildcard, which would let any
    #: caller forge the client IP that rate limiting throttles on and the audit trail records.
    forwarded_allow_ips: str = "127.0.0.1"
    #: Serve ``/docs``, ``/redoc`` and ``/openapi.json``. Unset means off in production:
    #: the schema enumerates every route and its shape for an unauthenticated caller.
    expose_api_docs: bool | None = None
    #: Serve ``/metrics`` unauthenticated. Unset means off in production; scrape it over the
    #: cluster network or put the monitoring layer in front of it.
    expose_metrics_endpoint: bool | None = None

    # --- Database -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./finops.db"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Auth ---------------------------------------------------------------
    jwt_secret: str = "change-me-in-production-please-use-vault"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 14
    bootstrap_admin_email: str = "admin@finops.local"
    bootstrap_admin_password: str = "ChangeMe!2026"
    mfa_issuer: str = "FinOps Command Center"
    #: Key for field-level encryption of secrets at rest, independent of ``jwt_secret``
    #: so that rotating the signing key does not strand every stored provider credential.
    #: Any string is accepted and stretched; 32 random bytes base64-encoded is the
    #: intended form. Unset falls back to ``jwt_secret`` for backwards compatibility,
    #: which production refuses to start on.
    secret_encryption_key: str | None = None
    #: Failed logins tolerated per minute from one source for one account before the
    #: endpoint returns 429. Successful logins never consume budget, so this throttles
    #: password guessing without throttling the people who know their password.
    login_failure_limit_per_minute: int = 10
    #: Seed the operator/approver/auditor/builder demo identities alongside the admin.
    #: They all share ``bootstrap_admin_password``, so production refuses to seed them.
    seed_demo_users: bool = True

    # Keycloak / OIDC (optional SSO)
    keycloak_url: str | None = None
    keycloak_realm: str | None = None
    keycloak_client_id: str | None = None
    keycloak_client_secret: str | None = None

    # --- Infrastructure adapters -------------------------------------------
    redis_url: str | None = None
    kafka_bootstrap_servers: str | None = None
    kafka_topic_prefix: str = "finops"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "finops_knowledge"
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: str | None = None
    elasticsearch_url: str | None = None
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "finops-artifacts"
    minio_secure: bool = False
    #: Treat a local-filesystem artifact store as a failed readiness check. The container
    #: mounts an ``emptyDir``, so a silent fallback loses every artifact on restart and
    #: shares none of them between replicas. Defaults on outside local/dev.
    require_durable_artifact_store: bool | None = None
    vault_addr: str | None = None
    vault_token: str | None = None
    vault_mount: str = "secret"

    # --- Observability ------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "finops-api"
    log_level: str = "INFO"
    log_json: bool = True
    prometheus_enabled: bool = True

    # --- Temporal / Celery --------------------------------------------------
    temporal_host: str | None = None
    temporal_namespace: str = "default"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- LLM providers ------------------------------------------------------
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    google_api_key: str | None = None
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    mistral_api_key: str | None = None
    mistral_base_url: str = "https://api.mistral.ai/v1"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    together_api_key: str | None = None  # Llama hosting
    together_base_url: str = "https://api.together.xyz/v1"
    groq_api_key: str | None = None  # fast inference for open-weight models
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openrouter_api_key: str | None = None  # one key in front of many providers
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ollama_base_url: str | None = None  # local Llama/Mistral
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bedrock_endpoint: str | None = None

    default_model: str = "claude-haiku-4-5"

    # --- NeMo Guardrails -----------------------------------------------------
    # Rail configurations live in app/guardrails/configs/<agent_key>/. Deterministic rails
    # run with no credentials; the LLM-backed self-check rails need a provider and are
    # removed from the configuration when none is set.
    nemo_guardrails_enabled: bool = True
    # Model used by the LLM-backed rails. Empty falls back to default_model. A small, cheap
    # model is the right choice here — rails classify, they do not write.
    guardrails_model: str = ""
    # Block the run when an input rail refuses. Turning this off records the finding and
    # lets the run continue, which is only appropriate while tuning a new rail.
    nemo_block_on_input_rail: bool = True
    # Run the LLM-backed self-check rails. They are also removed automatically when no
    # provider is configured; set this false to run deterministic rails only.
    nemo_llm_rails_enabled: bool = True

    # --- Operating locale ----------------------------------------------------
    # Regulated contact windows (for example the Fair Practices Code 08:00-19:00 rule for
    # collections) are expressed in the customer's local time, not in UTC.
    bank_timezone: str = "Asia/Kolkata"
    default_embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3

    # --- External data / tool APIs -----------------------------------------
    market_data_api_key: str | None = None  # Alpha Vantage compatible
    market_data_base_url: str = "https://www.alphavantage.co"
    news_api_key: str | None = None
    news_base_url: str = "https://newsapi.org/v2"
    sec_edgar_user_agent: str = "FinOps Command Center contact@finops.local"
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    confluence_base_url: str | None = None
    confluence_email: str | None = None
    confluence_api_token: str | None = None
    slack_bot_token: str | None = None
    github_token: str | None = None
    sharepoint_tenant_id: str | None = None
    sharepoint_client_id: str | None = None
    sharepoint_client_secret: str | None = None
    sharepoint_site_id: str | None = None
    msgraph_tenant_id: str | None = None
    msgraph_client_id: str | None = None
    msgraph_client_secret: str | None = None

    # --- Governance ---------------------------------------------------------
    daily_cost_budget_usd: float = 500.0
    monthly_cost_budget_usd: float = 10000.0
    per_execution_cost_cap_usd: float = 5.0
    rate_limit_per_minute: int = 240
    execution_timeout_seconds: int = 900
    approval_timeout_seconds: int = 86400
    data_retention_days: int = 400
    max_concurrent_executions: int = 32

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_origins(cls, value: Any, info: ValidationInfo) -> list[str]:
        """Accept a comma-separated list, a JSON array, or a real list.

        Deployments write `CORS_ORIGINS=https://a.example,https://b.example`; Helm and
        Compose both produce that form. A JSON array is accepted too so existing
        configurations keep working.
        """
        if value is None or value == "":
            return []
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{(info.field_name or '').upper()} looks like JSON but will not "
                        f"parse: {exc}. Use a comma-separated list instead, for example "
                        "https://execute.example.com,https://console.example.com"
                    ) from exc
                return [str(item).strip() for item in decoded if str(item).strip()]
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def api_docs_enabled(self) -> bool:
        return not self.is_production if self.expose_api_docs is None else self.expose_api_docs

    @property
    def metrics_endpoint_enabled(self) -> bool:
        if self.expose_metrics_endpoint is None:
            return not self.is_production
        return self.expose_metrics_endpoint

    @property
    def durable_artifact_store_required(self) -> bool:
        if self.require_durable_artifact_store is not None:
            return self.require_durable_artifact_store
        return self.environment in {"staging", "production"}

    @property
    def secret_encryption_material(self) -> tuple[str, bool]:
        """The key used to encrypt secrets at rest, and whether it is the JWT fallback."""
        if self.secret_encryption_key:
            return self.secret_encryption_key, False
        return self.jwt_secret, True

    def production_misconfigurations(self) -> list[str]:
        """Settings that must not reach production at their shipped defaults.

        Each of these is a control that silently does nothing when left alone: a default
        signing key forges its own tokens, a wildcard host answers to any name, and a
        shared bootstrap password hands five roles to whoever reads the example file.
        Startup refuses rather than serving traffic that looks protected and is not.
        """
        defaults = Settings.model_fields
        problems: list[str] = []
        if self.jwt_secret == defaults["jwt_secret"].default:
            problems.append("JWT_SECRET is still the shipped default")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET is shorter than 32 characters")
        if not self.secret_encryption_key:
            problems.append(
                "SECRET_ENCRYPTION_KEY is unset, so secrets at rest are keyed off JWT_SECRET "
                "and rotating it would strand every stored credential"
            )
        if self.bootstrap_admin_password == defaults["bootstrap_admin_password"].default:
            problems.append("BOOTSTRAP_ADMIN_PASSWORD is still the shipped default")
        if self.seed_demo_users:
            problems.append(
                "SEED_DEMO_USERS is on, which would create operator/approver/auditor/builder "
                "accounts sharing BOOTSTRAP_ADMIN_PASSWORD"
            )
        if "*" in self.trusted_hosts:
            problems.append("TRUSTED_HOSTS contains '*'; set the hostnames this deployment serves")
        if "*" in self.forwarded_allow_ips:
            problems.append(
                "FORWARDED_ALLOW_IPS contains '*'; set the ingress addresses whose "
                "X-Forwarded-For this deployment should believe"
            )
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS contains '*'; set the browser origins that may call the API")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
