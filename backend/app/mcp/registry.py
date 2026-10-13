"""Adapting MCP tools into the platform's own tool registry.

This is the whole design: an MCP tool becomes an ordinary `Tool`, so it is reached through
the same `registry.invoke` as a built-in one and inherits the approval gate, the permission
check, the circuit breaker, the retry policy, the timing, the metrics and the audit trail
without any of them being reimplemented for the external case.

Three defaults are deliberately strict, because the tool on the other end was written by
someone else:

* **Every external tool requires a human decision**, unless an operator has named it
  read-only in configuration. `REVIEW_EXEMPT_WRITES` exempts built-in writes we audited one
  by one; nothing about a third-party tool is audited, so the safe assumption is that it
  writes, and being wrong in that direction costs a click.
* **Names are namespaced** `mcp.<server>.<tool>`. A server cannot advertise a tool called
  `record_credit_decision` and have an agent reach it believing it is ours -- registration
  refuses a collision outright rather than resolving it.
* **Nothing is adopted that an operator did not configure.** Servers come from settings, not
  from a model, a request or a database row an agent can write.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.core.config import settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.rbac import Permission
from app.mcp.client import MCPClient, MCPServerConfig, MCPToolDescriptor
from app.tools.base import Tool, ToolContext, registry

log = get_logger("mcp")

MCP_TOOL_PREFIX = "mcp."

#: Servers are addressed by name in logs, audit records and approval payloads, so the name
#: has to be a safe identifier rather than whatever a config file contains.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")

#: JSON Schema primitives we can map onto a Python type for argument validation. Anything
#: else is accepted as `Any`: rejecting a schema we do not understand would make this client
#: the thing that breaks when a server uses a feature it is entitled to use.
_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{MCP_TOOL_PREFIX}{server}.{tool}"


def configured_servers() -> list[MCPServerConfig]:
    """The MCP servers an operator has configured, with tokens resolved from the environment.

    `MCP_SERVERS` is a JSON array. A token is given as the *name* of an environment variable
    (`token_env`), never as a literal, so a server credential is not sitting in the same
    settings blob that gets printed into a diagnostic.
    """
    raw = (settings.mcp_servers or "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("mcp_servers_unparseable", error=str(exc))
        return []
    if not isinstance(entries, list):
        log.error("mcp_servers_not_a_list", got=type(entries).__name__)
        return []

    configs: list[MCPServerConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        url = str(entry.get("url") or "").strip()
        if not _SAFE_NAME.match(name):
            log.error("mcp_server_name_rejected", name=name)
            continue
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            # Tool arguments can carry customer data and the reply steers an agent, so the
            # hop is TLS-protected. Loopback is allowed so a server can be developed here.
            log.error("mcp_server_url_rejected", server=name, url=url)
            continue
        token_env = str(entry.get("token_env") or "").strip()
        token = os.environ.get(token_env) if token_env else None
        if token_env and not token:
            log.warning("mcp_server_token_missing", server=name, variable=token_env)
        configs.append(
            MCPServerConfig(
                name=name,
                url=url,
                token=token,
                headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                timeout_seconds=float(entry.get("timeout_seconds") or 30.0),
                read_only_tools=tuple(str(t) for t in (entry.get("read_only_tools") or [])),
                allowed_tools=tuple(str(t) for t in (entry.get("allowed_tools") or [])),
            )
        )
    return configs


def _field(schema: dict[str, Any], required: bool) -> tuple[Any, Any]:
    declared = schema.get("type")
    if isinstance(declared, list):  # ["string", "null"] and friends
        declared = next((d for d in declared if d != "null"), None)
    python_type = _JSON_TYPES.get(str(declared), Any)
    description = str(schema.get("description") or "")[:400]
    if required:
        return (python_type, Field(description=description))
    return (
        python_type | None if python_type is not Any else Any,
        Field(default=None, description=description),
    )


def args_model_for(descriptor: MCPToolDescriptor) -> type[BaseModel]:
    """Build a validating model from the server's declared input schema.

    Arguments are validated on our side before the call goes out, so a malformed tool call
    from the model is rejected here -- with the same error shape as any other tool -- rather
    than becoming a 400 from someone else's service halfway through an execution.
    """
    schema = descriptor.input_schema or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        # No declared schema: pass arguments through, still as a model so the invoke path
        # is identical. `extra="allow"` is what makes an undeclared argument survive.
        return create_model(
            f"MCPArgs_{descriptor.server}_{descriptor.name}",
            __config__=ConfigDict(extra="allow"),
        )

    required = {str(r) for r in (schema.get("required") or [])}
    fields: dict[str, Any] = {}
    for key, raw in properties.items():
        safe = str(key)
        if not safe.isidentifier():
            # A property name that is not a Python identifier cannot become a field; the
            # argument still reaches the server through `extra`.
            continue
        fields[safe] = _field(raw if isinstance(raw, dict) else {}, safe in required)

    return create_model(
        f"MCPArgs_{descriptor.server}_{descriptor.name}",
        __config__=ConfigDict(extra="allow"),
        **fields,
    )


def _handler(client: MCPClient, descriptor: MCPToolDescriptor) -> Any:
    async def call(args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        arguments = args.model_dump(exclude_none=True)
        log.info(
            "mcp_tool_call",
            server=descriptor.server,
            tool=descriptor.name,
            execution=ctx.execution_id,
            agent=ctx.agent_key,
            arguments=sorted(arguments),  # names, not values: arguments carry customer data
        )
        return await client.call_tool(descriptor.name, arguments)

    return call


def build_tool(client: MCPClient, descriptor: MCPToolDescriptor) -> Tool:
    name = mcp_tool_name(descriptor.server, descriptor.name)
    return Tool(
        name=name,
        # The attribution is part of the prompt on purpose: the model should know this tool
        # came from somewhere else, and so should anyone reading a trace.
        description=(
            f"[MCP · {descriptor.server}] {descriptor.description}"
            if descriptor.description
            else f"[MCP · {descriptor.server}] {descriptor.name}"
        ),
        handler=_handler(client, descriptor),
        args_model=args_model_for(descriptor),
        category="mcp",
        # A tool we cannot read is a tool a human decides on. `read_only_tools` is the
        # operator saying they read it.
        requires_approval=not descriptor.read_only,
        approval_risk="high" if not descriptor.read_only else "low",
        writes_data=not descriptor.read_only,
        required_permission=str(Permission.TOOL_INVOKE_EXTERNAL),
        timeout_seconds=client.config.timeout_seconds,
        # Not idempotent unless declared read-only: retrying someone else's write is how
        # one payment becomes two.
        idempotent=descriptor.read_only,
        max_retries=2 if descriptor.read_only else 1,
        tags=["mcp", descriptor.server],
    )


def registered_mcp_tools() -> list[Tool]:
    return [t for t in registry.all() if t.name.startswith(MCP_TOOL_PREFIX)]


#: Every adopted server keeps one client, held for the life of the process because its
#: connection pool and negotiated session are worth reusing across calls. They are tracked
#: so shutdown can close them: a connection pool nobody closes is a warning on every
#: restart and, under a reload loop, a socket leak.
_live_clients: list[MCPClient] = []


async def close_mcp_clients() -> None:
    while _live_clients:
        client = _live_clients.pop()
        try:
            await client.aclose()
        except Exception as exc:  # pragma: no cover - shutdown is best effort
            log.warning("mcp_client_close_failed", server=client.config.name, error=str(exc))


async def register_mcp_tools(
    configs: list[MCPServerConfig] | None = None,
    *,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Connect to each configured server and adopt its tools. Never raises.

    A third-party server being down must not stop this platform from starting, so every
    failure is reported in the result and logged. The caller gets a per-server summary it
    can put on a readiness endpoint.
    """
    servers = configs if configs is not None else configured_servers()
    summary: dict[str, Any] = {"servers": [], "tools_registered": 0}
    if not servers:
        return summary

    for config in servers:
        entry: dict[str, Any] = {"server": config.name, "url": config.url, "tools": [], "error": None}
        try:
            client = (client_factory or MCPClient)(config)
            _live_clients.append(client)
            await client.initialize()
            descriptors = await client.list_tools()
            # Checked before anything is adopted: a server reaching for our names does not
            # get a partial foothold while an operator works out why.
            assert_no_shadowing(config.name, [d.name for d in descriptors])
            entry["server_info"] = client.server_info
            entry["protocol_version"] = client.negotiated_version
            for descriptor in descriptors:
                tool = build_tool(client, descriptor)
                if registry.has(tool.name):
                    log.warning("mcp_tool_already_registered", tool=tool.name)
                    continue
                registry.register(tool)
                entry["tools"].append(
                    {
                        "name": tool.name,
                        "requires_human_review": tool.requires_human_review,
                        "read_only": descriptor.read_only,
                    }
                )
                summary["tools_registered"] += 1
            log.info(
                "mcp_server_adopted",
                server=config.name,
                tools=len(entry["tools"]),
                gated=sum(1 for t in entry["tools"] if t["requires_human_review"]),
            )
        except Exception as exc:
            entry["error"] = str(exc)
            log.error("mcp_server_unavailable", server=config.name, error=str(exc))
        summary["servers"].append(entry)
    return summary


def assert_no_shadowing(server: str, tools: list[str]) -> None:
    """Refuse a server that advertises the name of a built-in tool.

    Namespacing already prevents the collision, so this exists for the clearer failure: a
    server offering `record_credit_decision` is either confused or hostile, and either way
    an operator should hear about it rather than find an `mcp.*` twin in the catalogue.
    """
    collisions = [t for t in tools if registry.has(t)]
    if collisions:
        raise ValidationError(
            f"MCP server '{server}' advertises tools whose names belong to built-in tools",
            details={"server": server, "collisions": sorted(collisions)},
        )
