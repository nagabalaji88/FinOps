"""A JSON-RPC client for MCP servers over Streamable HTTP.

Only the three methods this platform needs are implemented -- `initialize`, `tools/list`,
`tools/call` -- because the surface we expose to an agent is tools, and a client that could
also read resources or run prompts would be a capability nobody asked for and nobody gated.

Deliberately written against the wire protocol rather than an SDK: the dependency closure of
this service is audited on every build, and a transport this small does not justify adding
to it. The protocol version is pinned and a server that negotiates a different one is
reported rather than assumed compatible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redaction import redact_provider_body

log = get_logger("mcp")

#: The revision this client is written against. A server that answers with a different one
#: still works for `tools/list` and `tools/call` in practice, so this is logged rather than
#: enforced -- but it is recorded, so a behaviour change has something to point at.
PROTOCOL_VERSION = "2025-06-18"

CLIENT_INFO = {"name": "finops-command-center", "version": "1.0.0"}

#: A description written by a third party is injected verbatim into the model's prompt, so
#: it is an untrusted input on the most sensitive path there is. Capped, stripped of control
#: characters, and never allowed to carry the formatting that frames our own instructions.
MAX_DESCRIPTION_CHARS = 1_024


class MCPError(AppError):
    code = "mcp_error"
    status_code = 502


@dataclass(slots=True)
class MCPServerConfig:
    """One MCP server an operator has chosen to trust."""

    name: str
    url: str
    #: Bearer token, resolved from the environment by the caller -- never stored here.
    token: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    #: Tools on this server that only read. Everything else is treated as a write and is
    #: gated, because this client cannot know what a third-party tool does and guessing
    #: wrong in that direction is the expensive mistake.
    read_only_tools: tuple[str, ...] = ()
    #: When set, only these tools are adopted. A server that starts advertising a new tool
    #: cannot thereby hand an agent a new capability.
    allowed_tools: tuple[str, ...] = ()

    def is_allowed(self, tool: str) -> bool:
        return not self.allowed_tools or tool in self.allowed_tools


@dataclass(slots=True)
class MCPToolDescriptor:
    """A tool as the server describes it, after sanitisation."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


def _sanitise_description(raw: Any, *, server: str, tool: str) -> str:
    """Make a third party's prose safe to put in a prompt, without silently emptying it.

    The threat is a tool description that instructs the model rather than describing the
    tool -- "ignore previous instructions", or a fake system block. The rails screen the
    request and the response, but a tool schema reaches the model outside that path, so it
    is cleaned here and attributed to its server so the model sees it as third-party text.
    """
    text = str(raw or "").replace("\x00", "")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")
    # Backtick fences and XML-ish tags are how our own prompt frames authoritative blocks.
    text = text.replace("```", "'''").replace("<|", "<").replace("|>", ">")
    text = " ".join(text.split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rstrip() + " …"
        log.info("mcp_description_truncated", server=server, tool=tool)
    return text or f"Tool '{tool}' exposed by the MCP server '{server}' with no description."


def _parse_sse(body: str) -> dict[str, Any]:
    """Pull the JSON-RPC message out of an SSE response.

    Streamable HTTP lets a server answer a single request with an event stream, so a client
    that only read `response.json()` would fail against conformant servers.
    """
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    return dict(json.loads(payload))
                except json.JSONDecodeError:
                    continue
    raise MCPError("MCP server returned an event stream with no JSON-RPC message")


class MCPClient:
    """Talks to one MCP server. Cheap to construct; holds the negotiated session id."""

    def __init__(self, config: MCPServerConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self._session_id: str | None = None
        self._next_id = 0
        self._server_info: dict[str, Any] = {}
        self._negotiated_version: str | None = None

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def negotiated_version(self) -> str | None:
        return self._negotiated_version

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both, because a conformant server may answer either way.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.config.headers,
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            request["params"] = params

        try:
            response = await self._http().post(self.config.url, json=request, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPError(
                f"MCP server '{self.config.name}' is unreachable: {exc}",
                details={"server": self.config.name, "method": method},
            ) from exc

        if response.status_code >= 400:
            # The body may echo the token or arguments we sent, so it is redacted before it
            # reaches a log line or an error a caller might surface.
            raise MCPError(
                f"MCP server '{self.config.name}' returned {response.status_code} for {method}",
                details={
                    "server": self.config.name,
                    "method": method,
                    "status": response.status_code,
                    "body": redact_provider_body(response.text, limit=500),
                },
            )

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            message = _parse_sse(response.text)
        else:
            try:
                message = dict(response.json())
            except (json.JSONDecodeError, ValueError) as exc:
                raise MCPError(
                    f"MCP server '{self.config.name}' returned a non-JSON body for {method}"
                ) from exc

        if "error" in message:
            error = message["error"] or {}
            raise MCPError(
                f"MCP server '{self.config.name}' rejected {method}: {error.get('message', 'unknown error')}",
                details={"server": self.config.name, "method": method, "code": error.get("code")},
            )
        return dict(message.get("result") or {})

    async def _notify(self, method: str) -> None:
        """Fire-and-forget: a notification has no id and the server sends no reply."""
        try:
            await self._http().post(
                self.config.url,
                json={"jsonrpc": "2.0", "method": method},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:  # pragma: no cover - best effort by design
            log.warning("mcp_notify_failed", server=self.config.name, method=method, error=str(exc))

    async def initialize(self) -> dict[str, Any]:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # No capabilities are declared, because this client implements none of the
                # optional ones. A server that sees sampling or roots advertised would be
                # entitled to ask us to run a model on its behalf.
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._server_info = dict(result.get("serverInfo") or {})
        self._negotiated_version = result.get("protocolVersion")
        if self._negotiated_version and self._negotiated_version != PROTOCOL_VERSION:
            log.info(
                "mcp_protocol_version_differs",
                server=self.config.name,
                client=PROTOCOL_VERSION,
                server_version=self._negotiated_version,
            )
        await self._notify("notifications/initialized")
        log.info(
            "mcp_initialized",
            server=self.config.name,
            name=self._server_info.get("name"),
            version=self._server_info.get("version"),
            protocol=self._negotiated_version,
        )
        return result

    async def list_tools(self) -> list[MCPToolDescriptor]:
        """Every tool the server advertises that the operator has allowed."""
        descriptors: list[MCPToolDescriptor] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(20):  # a server that paginates forever does not get to hang a boot
            params = {"cursor": cursor} if cursor else {}
            result = await self._rpc("tools/list", params)
            for raw in result.get("tools") or []:
                name = str(raw.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                if not self.config.is_allowed(name):
                    log.info("mcp_tool_not_allowed", server=self.config.name, tool=name)
                    continue
                schema = raw.get("inputSchema")
                descriptors.append(
                    MCPToolDescriptor(
                        server=self.config.name,
                        name=name,
                        description=_sanitise_description(
                            raw.get("description"), server=self.config.name, tool=name
                        ),
                        input_schema=dict(schema) if isinstance(schema, dict) else {},
                        read_only=name in self.config.read_only_tools,
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return descriptors

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool and return its content, with `isError` surfaced as an error.

        An MCP server reports a tool failure inside a successful response, so a client that
        only checked the transport would record a failed call as a success and hand the
        model the error text as though it were a result.
        """
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()

        if result.get("isError"):
            raise MCPError(
                f"MCP tool '{name}' on '{self.config.name}' failed: {text or 'no detail given'}",
                details={"server": self.config.name, "tool": name},
            )

        payload: dict[str, Any] = {
            "server": self.config.name,
            "tool": name,
            "content": content,
        }
        if text:
            payload["text"] = text
        if "structuredContent" in result:
            payload["structured"] = result["structuredContent"]
        return payload

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
