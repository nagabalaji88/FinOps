"""Declarative base, portable column types and common mixins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, JSON, String, Text, TypeDecorator, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on PostgreSQL, JSON elsewhere -> identical Python API, better indexing in prod.
JSONType = JSON().with_variant(JSONB, "postgresql")


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on every dialect.

    SQLite has no native timezone support and returns naive values; this decorator
    normalises on the way in and re-attaches UTC on the way out so application code
    never has to compare naive and aware datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[str]: JSONType,
        list[dict[str, Any]]: JSONType,
        float: Float,
        str: String(255),
    }

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        exclude = exclude or set()
        out: dict[str, Any] = {}
        for column in self.__table__.columns:
            if column.name in exclude:
                continue
            value = getattr(self, column.name)
            out[column.name] = value.isoformat() if isinstance(value, datetime) else value
        return out


class UUIDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)


LongText = Text
