"""Model Context Protocol client: third-party tool servers, under this platform's controls.

MCP is how the ecosystem now ships tools, and the point of consuming it here is *not* to
hand agents a second, parallel tool path. Tools are the only way an agent reaches a system
of record, and everything that makes that safe -- the human-approval gate, the permission
check, the audit trail, the cost ledger, the circuit breaker -- lives in the tool registry.

So an MCP server's tools are adapted into ordinary `Tool` objects and registered there. An
external tool is reached through the same `registry.invoke` as a built-in one, which means
it inherits every control rather than needing them reimplemented, and an operator reviewing
an execution cannot tell from the audit trail that they had to be.
"""

from __future__ import annotations

from app.mcp.client import MCPClient, MCPError, MCPServerConfig, MCPToolDescriptor
from app.mcp.registry import (
    MCP_TOOL_PREFIX,
    close_mcp_clients,
    configured_servers,
    mcp_tool_name,
    register_mcp_tools,
    registered_mcp_tools,
)

__all__ = [
    "MCP_TOOL_PREFIX",
    "MCPClient",
    "MCPError",
    "MCPServerConfig",
    "MCPToolDescriptor",
    "close_mcp_clients",
    "configured_servers",
    "mcp_tool_name",
    "register_mcp_tools",
    "registered_mcp_tools",
]
