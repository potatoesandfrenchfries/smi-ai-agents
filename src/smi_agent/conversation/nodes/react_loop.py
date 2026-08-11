"""react_loop_node — ReAct tool-calling agent loop for conversation agents.

Runs between context_inject and generate. The LLM decides which tools to call
(Neo4j graph queries, Postgres data queries) based on the user's question,
executes them, feeds results back, and repeats until the LLM responds with
content or the budget is exhausted.

Flow:
  1. Build tool definitions from ToolRegistry
  2. Send messages + tools to LLM
  3. If LLM returns tool_calls → execute, append results, loop
  4. If LLM returns content (stop) → save accumulated context, exit
  5. Budget: each LLM call counts toward max_llm_calls
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from smi_agent.config.models import AgentDefinition
from smi_agent.conversation.state import ConversationState
from smi_agent.conversation.tools.registry import ToolRegistry
from smi_agent.llm.prompts import PromptLoader
from smi_agent.llm.router import LLMRouter
from smi_agent.mcp_client.client import get_mcp_client
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.safe_executor import SafeCypherExecutor
from smi_agent.neo4j_client.templates import TemplateLoader
from smi_agent.postgres_client.safe_executor import SafePostgresExecutor
from smi_agent.streaming import get_step_emitter

logger = logging.getLogger(__name__)


def make_react_loop_node(
    defn: AgentDefinition,
    driver: Neo4jDriver,
    template_loader: TemplateLoader,
    pg_client: Any | None = None,
    prompt_loader: PromptLoader | None = None,
):
    """Factory: creates the ReAct tool-calling loop node."""

    _tc = defn.tool_calling
    _lane = defn.llm.lane
    _temperature = defn.llm.temperature
    _max_iterations = _tc.max_tool_iterations
    _max_calls = _tc.max_llm_calls

    async def react_loop_node(
        state: ConversationState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        config = config or {}
        emitter = get_step_emitter(config, state)
        errors: list[str] = list(state.get("errors") or [])
        history: list[dict[str, Any]] = list(state.get("history") or [])
        user_message: str = state["user_message"]
        page_context = state.get("page_context")

        # Build executors with agent's allowlist
        cypher_exec = None
        if driver and defn.graph.allowed_cypher_templates:
            cypher_exec = SafeCypherExecutor(
                driver=driver,
                allowed_templates=defn.graph.allowed_cypher_templates,
                agent_name=defn.name,
                template_loader=template_loader,
                graph_policy=defn.graph,
            )

        pg_exec = None
        if pg_client is not None:
            pg_exec = SafePostgresExecutor(
                client=pg_client,
                allowed_queries=defn.postgres.allowed_queries,
                agent_name=defn.name,
            )

        # Build tool registry
        registry = ToolRegistry(
            cypher_executor=cypher_exec,
            postgres_executor=pg_exec,
            template_loader=template_loader,
            enabled_tools=_tc.enabled_tools or None,
            mcp_client=get_mcp_client(),
        )
        await registry.load_mcp_tools()

        tools = registry.openai_tool_definitions()
        if not tools:
            # No tools available — skip loop
            return {
                "tool_context": [],
                "tool_iterations": 0,
                "errors": errors,
                "current_node": "react_loop",
            }

        # Build LLM router
        router = LLMRouter(lane=_lane, temperature=_temperature)

        # Build system prompt with page context
        page_type = page_context.page_type if page_context else "unknown"
        entity_id = (page_context.entity_id or "") if page_context else ""
        snapshot = (page_context.entity_snapshot or {}) if page_context else {}

        # Extract tenant_id from state or use default
        tenant_id = state.get("tenant_id") or "00000000-0000-0000-0000-000000000001"

        system_text = (
            f"You are an AI assistant.\n"
            f"Page: {page_type} | Entity: {entity_id}\n"
            f"Tenant ID: {tenant_id}\n"
        )
        if snapshot:
            system_text += f"\nContext:\n{json.dumps(snapshot, default=str)[:2000]}\n"
        system_text += (
            "\nYou have access to graph and database tools. "
            "Use them to answer the user's question with specific data. "
            "Call tools when you need data you don't already have. "
            "When you have enough data, respond directly without calling more tools.\n"
            "\nIMPORTANT tool parameter rules:\n"
            f"- For Postgres queries that need tenant_id, ALWAYS use: {tenant_id}\n"
            f"- For Postgres queries that need entity_id, use: {entity_id}\n"
            "- NEVER output SQL or Cypher queries to the user.\n"
            "- NEVER use emojis.\n"
        )

        # Build message history for the loop
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
        ]
        for msg in history[-10:]:  # Last 10 messages for context
            role = msg.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user_message})

        # ── ReAct Loop ────────────────────────────────────────────────────────
        tool_context: list[dict[str, Any]] = []
        calls_made = 0

        for iteration in range(_max_iterations):
            if calls_made >= _max_calls:
                logger.info("react_loop: budget exhausted (%d calls)", calls_made)
                break

            # Call LLM with tools
            try:
                result = await router.call(
                    messages=messages,
                    trace_name=f"react_loop_iter_{iteration}",
                    tools=tools,
                )
                calls_made += 1
            except Exception as exc:
                logger.warning("react_loop: LLM call failed: %s", exc)
                errors.append(f"react_loop: LLM error: {type(exc).__name__}")
                break

            # Check if LLM wants to call tools
            if not result.tool_calls:
                # LLM responded with content — we're done
                if result.content:
                    tool_context.append({
                        "tool_name": "_llm_summary",
                        "result": result.content,
                    })
                break

            # Execute tool calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": result.tool_calls,
            }
            messages.append(assistant_msg)

            for tc in result.tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                await emitter.emit(
                    "react_loop",
                    "in_progress",
                    f"Querying {tool_name}...",
                    detail={"tool": tool_name, "iteration": iteration},
                )

                tool_result = await registry.execute(tc)

                # Append tool result to messages (OpenAI format)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_result.tool_call_id,
                    "content": tool_result.content,
                })

                # Save to tool_context for generate node
                tool_context.append({
                    "tool_name": tool_name,
                    "result": tool_result.content[:2000],
                    "success": tool_result.success,
                })

                logger.info(
                    "react_loop: iter=%d tool=%s success=%s len=%d",
                    iteration, tool_name, tool_result.success, len(tool_result.content),
                )

        if tool_context:
            await emitter.emit(
                "react_loop",
                "completed",
                f"Retrieved data from {len(tool_context)} source(s)",
            )

        return {
            "tool_context": tool_context,
            "tool_iterations": calls_made,
            "errors": errors,
            "current_node": "react_loop",
        }

    return react_loop_node
