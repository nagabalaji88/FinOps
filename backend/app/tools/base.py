"""Tool contract and registry.

Tools are the only way agents touch systems of record. Every invocation is
schema-validated, timed, metered, health-tracked and audit-logged, and tools flagged
``requires_approval`` suspend the execution until a human decides.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.metrics import tool_calls_total, tool_latency
from app.core.resilience import RetryPolicy, get_breaker, with_retry
from app.llm.types import ToolSchema

log = get_logger("tools")

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class ToolContext:
    """Everything a tool needs about the caller and the surrounding execution."""

    execution_id: str | None = None
    agent_key: str | None = None
    user_id: str | None = None
    user_email: str | None = None
    roles: list[str] = field(default_factory=list)
    correlation_id: str | None = None
    trace_id: str | None = None
    session: Any = None  # AsyncSession, injected by the engine
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    tool: str = ""
    retries: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "retries": self.retries,
            "metadata": self.metadata,
        }


@dataclass
class Tool:
    name: str
    description: str
    handler: ToolHandler
    args_model: type[BaseModel]
    category: str = "internal"
    requires_approval: bool = False
    approval_risk: str = "medium"
    required_permission: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 2
    idempotent: bool = True
    writes_data: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        # Inline $defs so providers that reject $ref still accept the schema.
        defs = schema.pop("$defs", None)
        if defs:
            schema = _inline_refs(schema, defs)
        return schema

    def to_llm_schema(self) -> ToolSchema:
        return ToolSchema(name=self.name, description=self.description, parameters=self.json_schema)


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node and node["$ref"].startswith("#/$defs/"):
            target = defs.get(node["$ref"].split("/")[-1], {})
            return _inline_refs({k: v for k, v in target.items()}, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._health_hook: Callable[[str, ToolResult, str], Awaitable[None]] | None = None

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool
        return tool

    def set_health_hook(self, hook: Callable[[str, ToolResult, str], Awaitable[None]]) -> None:
        self._health_hook = hook

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(f"Tool '{name}' is not registered",
                                details={"available": sorted(self._tools)})
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def by_names(self, names: list[str]) -> list[Tool]:
        return [self._tools[n] for n in names if n in self._tools]

    def schemas_for(self, names: list[str]) -> list[ToolSchema]:
        return [t.to_llm_schema() for t in self.by_names(names)]

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        started = time.perf_counter()
        retries = 0

        try:
            args = tool.args_model(**(arguments or {}))
        except PydanticValidationError as exc:
            result = ToolResult(
                ok=False, tool=name, error=f"Invalid arguments: {exc.errors()}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            tool_calls_total.labels(name, "invalid_args").inc()
            await self._record(tool, result, "invalid_args")
            return result

        breaker = get_breaker(f"tool:{name}", failure_threshold=6, recovery_seconds=20)

        def _on_retry(attempt: int, _exc: BaseException) -> None:
            nonlocal retries
            retries = attempt

        async def _run() -> Any:
            call = tool.handler(args, ctx)
            if not inspect.isawaitable(call):
                raise TypeError(f"Tool '{name}' handler must be async")
            return await asyncio.wait_for(call, timeout=tool.timeout_seconds)

        try:
            data = await with_retry(
                lambda: breaker.call(_run),
                RetryPolicy(
                    max_attempts=tool.max_retries if tool.idempotent else 1,
                    base_delay=0.3,
                    give_up_on=(ValidationError, NotFoundError, TimeoutError, asyncio.TimeoutError),
                ),
                on_retry=_on_retry,
                name=f"tool:{name}",
            )
            latency = (time.perf_counter() - started) * 1000
            result = ToolResult(ok=True, data=data, latency_ms=latency, tool=name, retries=retries)
            tool_calls_total.labels(name, "success").inc()
            tool_latency.labels(name).observe(latency / 1000)
            await self._record(tool, result, "healthy")
            return result
        except TimeoutError:
            latency = (time.perf_counter() - started) * 1000
            result = ToolResult(ok=False, tool=name, error=f"Tool timed out after "
                                f"{tool.timeout_seconds}s", latency_ms=latency, retries=retries)
            tool_calls_total.labels(name, "timeout").inc()
            await self._record(tool, result, "timeout")
            return result
        except AppError as exc:
            latency = (time.perf_counter() - started) * 1000
            result = ToolResult(ok=False, tool=name, error=exc.message, latency_ms=latency,
                                retries=retries, metadata={"code": exc.code, **exc.details})
            tool_calls_total.labels(name, "error").inc()
            await self._record(tool, result, "degraded")
            return result
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000
            log.exception("tool_failed", tool=name, error=str(exc))
            result = ToolResult(ok=False, tool=name, error=str(exc), latency_ms=latency,
                                retries=retries)
            tool_calls_total.labels(name, "error").inc()
            await self._record(tool, result, "unhealthy")
            return result

    async def _record(self, tool: Tool, result: ToolResult, status: str) -> None:
        if self._health_hook is None:
            return
        try:
            await self._health_hook(tool.name, result, status)
        except Exception as exc:  # pragma: no cover
            log.warning("tool_health_hook_failed", tool=tool.name, error=str(exc))


registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    args_model: type[BaseModel],
    **kwargs: Any,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator registering an async function as a tool."""

    def decorator(fn: ToolHandler) -> ToolHandler:
        registry.register(
            Tool(name=name, description=description, handler=fn, args_model=args_model, **kwargs)
        )
        return fn

    return decorator
