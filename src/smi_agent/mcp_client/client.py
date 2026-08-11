"""MCP client — talks to the smi-mcp-server process (mcp_server/server.py)
over Streamable HTTP.

Both orchestration paths call through this module instead of importing
providers/registry.py directly:
  - conversation/tools/registry.py::ToolRegistry (agents/ path — dynamic
    LLM tool-calling)
  - graph/itinerary_graph.py + activities/travel_activities.py (Temporal/
    LangGraph path — fixed pipeline, same MCP boundary for decoupling)

A fresh connection is opened per call rather than held open across calls —
callers here are infrequent (one search per itinerary stage) and this way
nothing needs a long-lived session/reconnect story, matching this
codebase's other prototype-scope network clients (e.g. examples/travel/
tools/*_scraper.py's per-call httpx.AsyncClient).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class MCPToolError(RuntimeError):
    """Raised when an MCP tool call fails or the tool itself reports isError."""


class MCPClient:
    def __init__(self, server_url: str | None = None) -> None:
        self._url = server_url or os.environ.get("SMI_MCP_SERVER_URL", "http://localhost:9200/mcp")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool definitions: [{"name", "description", "input_schema"}, ...]."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            streamable_http_client(self._url) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            return [
                {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                for t in result.tools
            ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool and return its structured result (unwrapped).

        Tools here return list[dict]/dict — the MCP SDK wraps non-object
        JSON-Schema results as {"result": <value>} in structuredContent, so
        a list-returning tool like search_flights needs unwrapping before
        callers get back the same shape providers/registry.py always
        returned.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            streamable_http_client(self._url) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)

            if result.is_error:
                text = "; ".join(c.text for c in result.content if hasattr(c, "text"))
                raise MCPToolError(f"MCP tool {name!r} failed: {text}")

            content = result.structured_content
            if isinstance(content, dict) and set(content.keys()) == {"result"}:
                return content["result"]
            return content


_client: MCPClient | None = None


def get_mcp_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client
