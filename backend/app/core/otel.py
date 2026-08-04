"""OpenTelemetry bootstrap. Exports to OTLP (Jaeger/Tempo/Collector) when configured."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span, StatusCode

from app.core.config import settings

_initialised = False


def setup_tracing(app: Any = None, engine: Any = None) -> None:
    global _initialised
    if _initialised:
        return
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "1.0.0",
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
            )
        )
    elif settings.debug:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        except Exception:  # pragma: no cover - instrumentation is best effort
            pass
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception:  # pragma: no cover
        pass
    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
        except Exception:  # pragma: no cover
            pass
    _initialised = True


def get_tracer(name: str = "finops") -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_ids() -> tuple[str | None, str | None]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def record_exception(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(StatusCode.ERROR, str(exc))
