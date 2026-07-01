"""SupervisorAgent — intent classification and specialist routing.

The supervisor receives every user message, classifies intent, delegates
to the right specialist agent, and returns the structured response.

Pattern: Supervisor-as-tool-caller. Each specialist is registered as an
OpenAI function tool. The supervisor LLM decides which to call.

Flow:
  1. Build messages with specialist tool definitions
  2. Call LLM (middle lane, low temperature) with tools
  3. If tool_call → run specialist → return its response
  4. If content → wrap as text block (greetings, clarifications)
  5. Apply guardrails, log audit trail
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from smi_agent.agents.registry import AgentRegistry
from smi_agent.agents.response import (
    ResponseMeta,
    StructuredResponse,
    error_block,
    simple_response,
)
from smi_agent.llm.prompts import PromptLoader
from smi_agent.llm.router import LLMRouter
from smi_agent.streaming import NullStepEmitter, StepEmitter

logger = logging.getLogger(__name__)

# Circuit breaker cooldown (seconds)
_CIRCUIT_BREAKER_COOLDOWN = 60


class SupervisorAgent:
    """Main entry point for the multi-agent chat system.

    Usage:
        supervisor = SupervisorAgent(registry)
        response = await supervisor.handle("Show me top flights", history, context, emitter)
    """

    def __init__(
        self,
        agent_registry: AgentRegistry,
        prompt_loader: PromptLoader | None = None,
    ) -> None:
        self._registry = agent_registry
        self._router = LLMRouter(lane="middle", temperature=0.1)
        self._prompt_loader = prompt_loader or PromptLoader("prompts/agents/supervisor")
        self._circuit_breaker: dict[str, tuple[float, int]] = {}  # name → (failed_at, cooldown)

    async def handle(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        step_emitter: StepEmitter | None = None,
    ) -> StructuredResponse:
        """Handle a user message: classify → route → execute → return."""
        start_time = time.monotonic()
        history = history or []
        context = context or {}
        emitter = step_emitter or NullStepEmitter()

        await emitter.emit("supervisor", "in_progress", "Analyzing your query...")

        # Build specialist tool definitions (skip circuit-broken ones)
        specialist_tools = []
        available_specialists = []
        for specialist in self._registry.all_specialists():
            if self._is_circuit_broken(specialist.name):
                logger.info("Skipping circuit-broken specialist: %s", specialist.name)
                continue
            specialist_tools.append(specialist.as_tool_definition())
            available_specialists.append(specialist)

        # Add a direct-response tool for greetings/clarifications
        specialist_tools.append({
            "type": "function",
            "function": {
                "name": "respond_directly",
                "description": (
                    "Respond directly to the user without delegating to a specialist. "
                    "Use for greetings, clarifications, or when no specialist is needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Your response to the user. IMPORTANT: Do NOT use any emojis."},
                        "followUps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3-5 suggested follow-up questions",
                        },
                    },
                    "required": ["message"],
                },
            },
        })

        # Build supervisor system prompt
        specialist_list = "\n".join(
            f"- ask_{s.name}: {s.description}" for s in available_specialists
        )
        system_prompt = self._build_system_prompt(specialist_list, context)

        # Build messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        for msg in history[-10:]:
            role = msg.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user_message})

        # ── Call supervisor LLM ───────────────────────────────────────────
        try:
            result = await self._router.call(
                messages=messages,
                tools=specialist_tools,
                trace_name="supervisor_route",
            )
        except Exception as exc:
            logger.error("Supervisor LLM call failed: %s", exc)
            return simple_response(
                "supervisor",
                "I encountered an issue processing your request. Please try again.",
                status="error",
            )

        # ── Route based on LLM decision ──────────────────────────────────
        if not result.tool_calls:
            duration = int((time.monotonic() - start_time) * 1000)
            resp = simple_response("supervisor", result.content)
            resp.meta = ResponseMeta(durationMs=duration, modelUsed="middle")
            return resp

        tool_call = result.tool_calls[0]
        fn_name = tool_call.get("function", {}).get("name", "")
        fn_args = json.loads(tool_call.get("function", {}).get("arguments", "{}"))

        logger.info("[supervisor] ROUTE fn=%s query=%r", fn_name, fn_args.get("query", user_message)[:120])

        # Direct response (greetings, clarifications)
        if fn_name == "respond_directly":
            duration = int((time.monotonic() - start_time) * 1000)
            resp = simple_response("supervisor", fn_args.get("message", ""))
            resp.followUps = fn_args.get("followUps", [])
            resp.meta = ResponseMeta(durationMs=duration, modelUsed="middle")
            await emitter.emit("supervisor", "completed", "Ready")
            return resp

        # Specialist delegation
        specialist_name = fn_name.replace("ask_", "")
        specialist = self._registry.get(specialist_name)

        if not specialist:
            logger.warning("Supervisor routed to unknown specialist: %s", specialist_name)
            return simple_response(
                "supervisor",
                f"I tried to use the {specialist_name} specialist but it is not available.",
                status="error",
            )

        query = fn_args.get("query", user_message)
        await emitter.emit(
            "supervisor", "in_progress",
            "Gathering data...",
        )

        # ── Execute specialist ────────────────────────────────────────────
        try:
            response = await specialist.run(query, context, emitter)
        except Exception as exc:
            logger.error("Specialist %s failed: %s", specialist_name, exc)
            self._trip_circuit_breaker(specialist_name)

            response = StructuredResponse(
                agent=specialist_name,
                status="error",
                blocks=[
                    error_block(
                        "Specialist unavailable",
                        f"The {specialist_name.replace('_', ' ')} agent encountered an issue. Please try again.",
                        severity="warning",
                    ),
                ],
                thinking=[
                    f"Routed to {specialist_name}",
                    "Specialist encountered an error",
                ],
            )

        # ── Finalize ──────────────────────────────────────────────────────
        duration = int((time.monotonic() - start_time) * 1000)
        response.meta.durationMs = duration

        supervisor_thinking = [f"Routed to {specialist_name.replace('_', ' ')}"]
        response.thinking = supervisor_thinking + response.thinking

        return response

    def _build_system_prompt(self, specialist_list: str, context: dict[str, Any]) -> str:
        """Build the supervisor's system prompt."""
        tenant_id = context.get("tenant_id", "00000000-0000-0000-0000-000000000001")

        try:
            return self._prompt_loader.render_system(
                "system",
                {
                    "specialist_list": specialist_list,
                    "tenant_id": tenant_id,
                },
            )
        except Exception:
            return (
                f"You are an AI assistant supervisor.\n"
                f"Tenant: {tenant_id}\n\n"
                f"You have these specialist agents:\n{specialist_list}\n"
                f"- respond_directly: For greetings, clarifications, simple answers\n\n"
                f"Classify the user's intent and delegate to the right specialist.\n"
                f"For greetings or simple questions, use respond_directly.\n"
                f"IMPORTANT: Do NOT use any emojis anywhere — no exceptions.\n"
            )

    def _is_circuit_broken(self, name: str) -> bool:
        """Check if a specialist is in circuit breaker cooldown."""
        if name not in self._circuit_breaker:
            return False
        failed_at, cooldown, _count = self._circuit_breaker[name]
        if time.monotonic() - failed_at > cooldown:
            del self._circuit_breaker[name]
            return False
        return True

    def _trip_circuit_breaker(self, name: str) -> None:
        """Mark a specialist as temporarily unavailable.

        Uses consecutive failure count with exponential backoff:
        1st failure → 30s, 2nd → 60s, 3rd+ → 120s.
        Requires 2 consecutive failures to trip.
        """
        prev = self._circuit_breaker.get(name)
        if prev:
            _prev_at, _prev_cooldown, prev_count = prev
            count = prev_count + 1
        else:
            count = 1

        if count < 2:
            self._circuit_breaker[name] = (time.monotonic(), 0, count)
            logger.info("Specialist %s failure %d/2 — not yet tripped", name, count)
            return

        cooldown = min(_CIRCUIT_BREAKER_COOLDOWN * (2 ** (count - 2)), 300)
        self._circuit_breaker[name] = (time.monotonic(), cooldown, count)
        logger.warning(
            "Circuit breaker tripped for specialist: %s (count=%d, cooldown=%ds)",
            name, count, cooldown,
        )