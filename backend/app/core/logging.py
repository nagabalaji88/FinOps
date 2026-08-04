"""Structured logging with trace-correlated context."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
execution_id_ctx: ContextVar[str | None] = ContextVar("execution_id", default=None)


def _inject_context(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key, ctx in (
        ("request_id", request_id_ctx),
        ("trace_id", trace_id_ctx),
        ("user_id", user_id_ctx),
        ("execution_id", execution_id_ctx),
    ):
        value = ctx.get()
        if value:
            event_dict.setdefault(key, value)
    event_dict.setdefault("service", settings.otel_service_name)
    event_dict.setdefault("env", settings.environment)
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "aiosqlite"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "finops") -> structlog.BoundLogger:
    return structlog.get_logger(name)
