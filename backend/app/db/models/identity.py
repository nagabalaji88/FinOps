"""Users, API keys, audit trail, feature flags and stored secrets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str | None] = mapped_column(String(255), default=None)
    roles: Mapped[list[str]] = mapped_column(JSONType, default=list)
    department: Mapped[str] = mapped_column(String(120), default="Unassigned")
    cost_center: Mapped[str | None] = mapped_column(String(120), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    sso_subject: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    sso_provider: Mapped[str | None] = mapped_column(String(64), default=None)
    mfa_secret: Mapped[str | None] = mapped_column(String(255), default=None)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(160))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    hashed_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scopes: Mapped[list[str]] = mapped_column(JSONType, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    user: Mapped[User] = relationship(back_populates="api_keys")


class RefreshToken(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)


class AuditLog(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor_action", "actor_id", "action"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    actor_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    actor_email: Mapped[str | None] = mapped_column(String(255), default=None)
    actor_type: Mapped[str] = mapped_column(String(32), default="user")  # user | api_key | system
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    outcome: Mapped[str] = mapped_column(String(32), default="success")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class FeatureFlag(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rollout_percentage: Mapped[int] = mapped_column(Integer, default=100)
    targeting: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(255), default=None)


class StoredSecret(Base, UUIDMixin, TimestampMixin):
    """Encrypted-at-rest secret used when Vault is not configured."""

    __tablename__ = "stored_secrets"

    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), default="integration")
    sealed_value: Mapped[str] = mapped_column(Text)
    hint: Mapped[str] = mapped_column(String(64), default="")
    backend: Mapped[str] = mapped_column(String(32), default="database")
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    rotation_interval_days: Mapped[int] = mapped_column(Integer, default=90)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
