"""Application configuration.

Every external dependency is optional at runtime: when its connection settings are
absent the corresponding adapter falls back to an embedded implementation and the
service is reported as ``not_configured`` on the Connected Services dashboard.
Nothing is faked -- a capability that requires an unconfigured provider fails loudly.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
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
            "http://localhost:5173",   # platform console (dev)
            "http://localhost:5174",   # execute console (dev)
            "http://localhost:4173",
            "http://localhost:4174",
        ]
    )
    root_path: str = ""

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
    ollama_base_url: str | None = None  # local Llama/Mistral
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bedrock_endpoint: str | None = None

    default_model: str = "claude-sonnet-4-5"

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
    default_embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3

    # --- External data / tool APIs -----------------------------------------
    market_data_api_key: str | None = None            # Alpha Vantage compatible
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

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> list[str]:
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
                        f"CORS_ORIGINS looks like JSON but will not parse: {exc}. "
                        "Use a comma-separated list instead, for example "
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
        return (
            self.database_url.replace("+asyncpg", "+psycopg2")
            .replace("+aiosqlite", "")
        )


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
