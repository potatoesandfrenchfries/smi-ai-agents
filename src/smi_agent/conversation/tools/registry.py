"""ToolRegistry — builds OpenAI function-calling tool definitions from Cypher
templates, Postgres queries, and MCP tools, and executes tool calls via
SafeExecutors / the MCP client.

Used by the ReAct agent loop to give the LLM access to Neo4j graph traversal,
Postgres data queries, and MCP-exposed search tools (flight/hotel/
restaurant/weather/maps/budget, see mcp_server/server.py) during
conversation turns.

Domain integration:
  Domain-specific Postgres tool definitions are provided by the configured
  ``QueryProvider`` via ``DomainRegistry.query_provider().tool_definitions()``.
  Generic (framework-level) tool definitions are still resolved from the
  local ``_PG_TOOL_DEFS`` map.

MCP tools:
  Unlike the Cypher/Postgres/specialist sources, MCP tool discovery is a
  network call, so it can't happen inside __init__. Construct the registry,
  then ``await registry.load_mcp_tools()`` before use — both call sites
  (agents/registry.py::GenericSpecialist.run, conversation/nodes/
  react_loop.py) are already inside an async function.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from smi_agent.agents.specialists.base import BaseSpecialist
from smi_agent.domain.registry import DomainRegistry
from smi_agent.mcp_client.client import MCPClient, MCPToolError
from smi_agent.neo4j_client.safe_executor import SafeCypherExecutor
from smi_agent.neo4j_client.templates import TemplateLoader
from smi_agent.observability.logging import get_planner_trace_logger
from smi_agent.postgres_client.safe_executor import SafePostgresExecutor
from smi_agent.streaming import NullStepEmitter, StepEmitter

logger = logging.getLogger(__name__)
trace_logger = get_planner_trace_logger()

# Max characters for a single tool result (protects LLM context window)
_MAX_RESULT_CHARS = 4000


@dataclass
class ToolResult:
    """Result of executing a tool call."""

    tool_call_id: str
    name: str
    content: str  # JSON-serialized result or error message
    success: bool


class ToolRegistry:
    """Builds tool definitions and executes tool calls for the ReAct loop.

    Args:
        cypher_executor: SafeCypherExecutor for Neo4j queries.
        postgres_executor: SafePostgresExecutor for Postgres queries.
        template_loader: Loads Cypher template metadata for parameter schemas.
        enabled_tools: Allowlist of tool names. Empty = all allowed by executors.
        specialists: Sibling specialist agents to expose as ask_<name> tools —
            this is what lets a coordinator agent (e.g. the "planner"
            specialist) dynamically decide which specialists to delegate to,
            instead of a fixed pipeline deciding for it.
        specialist_context: Context dict forwarded to each delegated
            specialist's run() (tenant_id, entity_id, etc).
        step_emitter: Forwarded to delegated specialists for SSE step events.
        mcp_client: Client for the MCP tool server (mcp_server/server.py) —
            exposes search_flights/search_hotels/search_restaurants/
            get_weather/search_maps/check_budget. Tool discovery happens in
            load_mcp_tools(), not here (see module docstring).
    """

    def __init__(
        self,
        cypher_executor: SafeCypherExecutor | None = None,
        postgres_executor: SafePostgresExecutor | None = None,
        template_loader: TemplateLoader | None = None,
        enabled_tools: list[str] | None = None,
        default_args: dict[str, str] | None = None,
        redis_client: Any | None = None,
        specialists: list[BaseSpecialist] | None = None,
        specialist_context: dict[str, Any] | None = None,
        step_emitter: StepEmitter | None = None,
        mcp_client: MCPClient | None = None,
    ) -> None:
        self._cypher = cypher_executor
        self._pg = postgres_executor
        self._loader = template_loader
        self._enabled = set(enabled_tools) if enabled_tools else None
        self._default_args = default_args or {}
        self._redis = redis_client
        self._specialists: dict[str, BaseSpecialist] = {s.name: s for s in (specialists or [])}
        self._specialist_context = specialist_context or {}
        self._step_emitter = step_emitter or NullStepEmitter()
        self._mcp = mcp_client
        self._builtin_tools: dict[str, dict] = {}
        self._cypher_tools: dict[str, dict] = {}
        self._pg_tools: dict[str, dict] = {}
        self._specialist_tools: dict[str, dict] = {}
        self._mcp_tools: dict[str, dict] = {}
        self._build()

    async def load_mcp_tools(self) -> None:
        """Fetch tool definitions from the MCP server. Call once after construction.

        Best-effort: if the MCP server is unreachable, logs and leaves
        _mcp_tools empty rather than failing the whole registry — matches
        this codebase's fail-closed-on-the-feature/fail-open-on-availability
        convention elsewhere (e.g. providers/*_scraper.py falling back to
        mock data on fetch failure), so a down MCP server degrades to
        "no search tools" instead of breaking Cypher/Postgres/specialist
        tool-calling too.
        """
        if self._mcp is None:
            return
        try:
            tools = await self._mcp.list_tools()
        except Exception:
            logger.exception("Failed to load MCP tool definitions — MCP tools unavailable this turn")
            return

        for t in tools:
            name = t["name"]
            if self._enabled and name not in self._enabled:
                continue
            self._mcp_tools[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }

    def _build(self) -> None:
        """Build tool definitions from available executors."""
        # Built-in tools (Redis-backed)
        if self._redis and (not self._enabled or "get_user_context" in self._enabled):
            self._builtin_tools["get_user_context"] = {
                "type": "function",
                "function": {
                    "name": "get_user_context",
                    "description": "Get the current user's identity: name, role, tenant, and permissions. Use this to personalize responses.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }

        # Cypher templates
        if self._cypher and self._loader:
            for tpl_name in self._cypher._allowed_templates:
                if self._enabled and tpl_name not in self._enabled:
                    continue
                try:
                    tpl = self._loader[tpl_name]
                    self._cypher_tools[tpl_name] = _cypher_to_tool_def(tpl_name, tpl)
                except Exception:
                    logger.debug("Skipping cypher tool %s (load failed)", tpl_name)

        # Postgres read queries (domain-specific + generic)
        if self._pg:
            # Merge domain-provided tool definitions
            domain_tool_defs = DomainRegistry.query_provider().tool_definitions()

            from smi_agent.postgres_client.queries import QUERY_REGISTRY

            for qname, query in QUERY_REGISTRY.items():
                if not query.is_read:
                    continue
                if qname.startswith(("insert_", "update_", "next_", "count_")):
                    continue  # Skip write/internal queries
                if self._enabled and qname not in self._enabled:
                    continue
                if qname not in self._pg._allowed_queries:
                    continue

                # Use domain tool def if available, else fallback
                if qname in domain_tool_defs:
                    self._pg_tools[qname] = {
                        "type": "function",
                        "function": {"name": qname, **domain_tool_defs[qname]},
                    }
                else:
                    self._pg_tools[qname] = _postgres_to_tool_def(qname, query)

        # Sibling specialists, exposed as ask_<name> tools
        for spec_name, specialist in self._specialists.items():
            tool_name = f"ask_{spec_name}"
            if self._enabled and tool_name not in self._enabled:
                continue
            self._specialist_tools[tool_name] = specialist.as_tool_definition()

    def openai_tool_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI function-calling tool definitions."""
        return (
            list(self._builtin_tools.values())
            + list(self._cypher_tools.values())
            + list(self._pg_tools.values())
            + list(self._specialist_tools.values())
            + list(self._mcp_tools.values())
        )

    def tool_names(self) -> list[str]:
        """Return names of all available tools."""
        return (
            list(self._builtin_tools.keys())
            + list(self._cypher_tools.keys())
            + list(self._pg_tools.keys())
            + list(self._specialist_tools.keys())
            + list(self._mcp_tools.keys())
        )

    async def execute(self, tool_call: dict[str, Any]) -> ToolResult:
        """Execute a single tool call. Returns structured result."""
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        call_id = tool_call.get("id", "unknown")

        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError as exc:
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                content=f"Invalid arguments JSON: {exc}",
                success=False,
            )

        # Auto-inject default args (e.g., tenant_id)
        for key, value in self._default_args.items():
            current = args.get(key)
            if not current or current in ("<UNKNOWN>", "unknown", "null", ""):
                args[key] = value

        # Route to correct executor
        if name == "get_user_context" and self._redis:
            return await self._execute_get_user_context(call_id)
        if name in self._cypher_tools and self._cypher:
            return await self._execute_cypher(call_id, name, args)
        if name in self._pg_tools and self._pg:
            return await self._execute_postgres(call_id, name, args)
        if name in self._specialist_tools:
            return await self._execute_specialist(call_id, name, args)
        if name in self._mcp_tools and self._mcp:
            return await self._execute_mcp(call_id, name, args)

        return ToolResult(
            tool_call_id=call_id,
            name=name,
            content=f"Unknown tool: {name}",
            success=False,
        )

    async def _execute_get_user_context(self, call_id: str) -> ToolResult:
        """Get the current user's context from Redis."""
        try:
            from smi_agent.config.redis_keys import user_context_key

            user_id = self._default_args.get("user_id", "")
            if not user_id:
                return ToolResult(
                    tool_call_id=call_id, name="get_user_context",
                    content='{"user_name": "unknown", "roles": "unknown"}', success=True,
                )

            raw = await self._redis.get(user_context_key(user_id))
            if raw:
                return ToolResult(
                    tool_call_id=call_id, name="get_user_context",
                    content=raw.decode() if isinstance(raw, bytes) else raw, success=True,
                )
            return ToolResult(
                tool_call_id=call_id, name="get_user_context",
                content=json.dumps({"user_id": user_id, "user_name": "unknown", "note": "User context not cached yet"}),
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=call_id, name="get_user_context",
                content=f"Failed to get user context: {type(exc).__name__}", success=False,
            )

    async def _execute_cypher(
        self, call_id: str, name: str, args: dict[str, Any]
    ) -> ToolResult:
        """Execute a Cypher template tool."""
        try:
            rows = await self._cypher.run(name, **args)
            result_json = json.dumps(rows, default=str)
            if len(result_json) > _MAX_RESULT_CHARS:
                result_json = result_json[:_MAX_RESULT_CHARS] + "...[truncated]"
            return ToolResult(
                tool_call_id=call_id, name=name, content=result_json, success=True
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                content=f"Query failed: {type(exc).__name__}: {exc}",
                success=False,
            )

    async def _execute_postgres(
        self, call_id: str, name: str, args: dict[str, Any]
    ) -> ToolResult:
        """Execute a Postgres canned query tool."""
        try:
            # Merge domain + generic tool defs for parameter clamping
            all_tool_defs = {
                **_get_generic_pg_tool_defs(),
                **DomainRegistry.query_provider().tool_definitions(),
            }
            if name in all_tool_defs:
                props = all_tool_defs[name].get("parameters", {}).get("properties", {})
                for pname, pschema in props.items():
                    if pname in args and "maximum" in pschema:
                        args[pname] = min(int(args[pname]), pschema["maximum"])

            # Extract args in schema order
            if name in all_tool_defs:
                param_names = list(all_tool_defs[name].get("parameters", {}).get("properties", {}).keys())
                arg_values = [args.get(p) for p in param_names if args.get(p) is not None]
            else:
                arg_values = list(args.values())

            rows = await self._pg.run(name, *arg_values)
            result_json = json.dumps(rows, default=str)
            if len(result_json) > _MAX_RESULT_CHARS:
                result_json = result_json[:_MAX_RESULT_CHARS] + "...[truncated]"
            return ToolResult(
                tool_call_id=call_id, name=name, content=result_json, success=True
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                content=f"Query failed: {type(exc).__name__}: {exc}",
                success=False,
            )

    async def _execute_mcp(
        self, call_id: str, name: str, args: dict[str, Any]
    ) -> ToolResult:
        """Execute an MCP tool call (search_flights, check_budget, etc)."""
        try:
            result = await self._mcp.call_tool(name, args)
            result_json = json.dumps(result, default=str)
            if len(result_json) > _MAX_RESULT_CHARS:
                result_json = result_json[:_MAX_RESULT_CHARS] + "...[truncated]"
            return ToolResult(
                tool_call_id=call_id, name=name, content=result_json, success=True
            )
        except MCPToolError as exc:
            return ToolResult(
                tool_call_id=call_id, name=name, content=str(exc), success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                content=f"MCP call failed: {type(exc).__name__}: {exc}",
                success=False,
            )

    async def _execute_specialist(
        self, call_id: str, name: str, args: dict[str, Any]
    ) -> ToolResult:
        """Delegate a tool call to a sibling specialist (dynamic planner path).

        This is the actual "which agent to invoke" decision point: the calling
        agent's LLM chose this tool over the others, so every call here is
        logged to the planner trace log with the query and outcome.
        """
        spec_name = name.removeprefix("ask_")
        specialist = self._specialists.get(spec_name)
        query = args.get("query", "")

        if specialist is None:
            trace_logger.warning("DELEGATE -> %s: not found (args=%s)", spec_name, args)
            return ToolResult(
                tool_call_id=call_id, name=name,
                content=f"Unknown specialist: {spec_name}", success=False,
            )

        trace_logger.info("DELEGATE -> specialist=%s query=%r", spec_name, query[:160])
        try:
            response = await specialist.run(query, self._specialist_context, self._step_emitter)
        except Exception as exc:
            logger.exception("Specialist %s failed", spec_name)
            trace_logger.warning("DELEGATE <- specialist=%s FAILED: %s", spec_name, exc)
            return ToolResult(
                tool_call_id=call_id, name=name,
                content=f"Specialist {spec_name} failed: {exc}", success=False,
            )

        trace_logger.info(
            "DELEGATE <- specialist=%s status=%s blocks=%d",
            spec_name, response.status, len(response.blocks),
        )

        result_payload = {"status": response.status, "payload": response.payload}
        result_json = json.dumps(result_payload, default=str)
        if len(result_json) > _MAX_RESULT_CHARS:
            result_json = result_json[:_MAX_RESULT_CHARS] + "...[truncated]"

        return ToolResult(
            tool_call_id=call_id, name=name, content=result_json,
            success=response.status != "error",
        )


# ── Helper: Cypher template → OpenAI tool definition ─────────────────────────


def _cypher_to_tool_def(name: str, template: Any) -> dict[str, Any]:
    """Convert a CypherTemplate to an OpenAI function tool definition."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, spec in (template.parameters or {}).items():
        prop: dict[str, Any] = {"description": param_name}
        ptype = getattr(spec, "type", "str")
        if ptype == "int":
            prop["type"] = "integer"
            if hasattr(spec, "clamp") and spec.clamp:
                clamp = spec.clamp
                clamp_min = getattr(clamp, "min", None) if not isinstance(clamp, dict) else clamp.get("min")
                clamp_max = getattr(clamp, "max", None) if not isinstance(clamp, dict) else clamp.get("max")
                if clamp_min is not None:
                    prop["minimum"] = clamp_min
                if clamp_max is not None:
                    prop["maximum"] = clamp_max
        elif ptype == "list[str]":
            prop["type"] = "array"
            prop["items"] = {"type": "string"}
        else:
            prop["type"] = "string"

        if getattr(spec, "default", None) is not None:
            prop["default"] = spec.default

        properties[param_name] = prop
        if getattr(spec, "required", False):
            required.append(param_name)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": getattr(template, "description", "")
            or f"Execute Neo4j graph query: {name}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ── Helper: Postgres query → OpenAI tool definition (generic fallback) ───────


def _get_generic_pg_tool_defs() -> dict[str, dict[str, Any]]:
    """Generic (framework-level) tool definitions for conversation queries."""
    return {}


def _postgres_to_tool_def(name: str, query: Any) -> dict[str, Any]:
    """Convert a Postgres SqlQuery to an OpenAI function tool definition."""
    generic = _get_generic_pg_tool_defs()
    if name in generic:
        return {
            "type": "function",
            "function": {"name": name, **generic[name]},
        }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Execute Postgres query: {name}",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }