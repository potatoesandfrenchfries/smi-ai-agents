"""AgentRuntime — generic ReAct executor for specialist agents.

Decoupled from LangGraph state. Any specialist calls ``runtime.run()``
with a user message, history, and context. The runtime handles the
tool-calling loop and returns a ``StructuredResponse``.

The loop:
  1. Send messages + tool defs to LLM
  2. If LLM returns tool_calls → execute, emit step events, loop
  3. If LLM returns content → parse into StructuredResponse
  4. Budget enforcement per iteration

Domain integration:
  Tool → responseType mapping and follow-up keyword validation are
  delegated to ``DomainRegistry.tool_mapping()`` so each domain
  controls its own mappings.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, get_args

from pydantic import ValidationError

from smi_agent.agents.response import (
    ContentBlock,
    EntityCardContent,
    EntityField,
    ResponseMeta,
    ResponseType,
    SourceRef,
    StructuredResponse,
    simple_response,
    text_block,
)
from smi_agent.conversation.tools.registry import ToolRegistry
from smi_agent.domain.registry import DomainRegistry
from smi_agent.llm.router import LLMRouter
from smi_agent.observability.logging import get_planner_trace_logger
from smi_agent.streaming import NullStepEmitter, StepEmitter

logger = logging.getLogger(__name__)
trace_logger = get_planner_trace_logger()

# Derive valid response types from the Literal — single source of truth
_VALID_RESPONSE_TYPES: set[str] = set(get_args(ResponseType))

# Max result chars per tool call (protects context window)
_MAX_TOOL_RESULT = 4000

# Cap on in-process re-prompts when the final structured-output call returns
# malformed JSON — matches Temporal's own activity retry ceiling (see
# activities/itinerary_workflow.py::_default_retry, maximum_attempts=3) and
# graph/itinerary_graph.py's ReflectionAgent, which uses the same cap.
_MAX_STRUCTURED_OUTPUT_RETRIES = 3

# entity_card grounding (see _ground_entity_cards): keys excluded when
# auto-deriving displayed fields from a raw tool record, and the cap on how
# many fields a card shows.
_ENTITY_CARD_EXCLUDED_KEYS = {"id", "reason", "provenance"}
_MAX_ENTITY_CARD_FIELDS = 8

# Response schema hint injected into the final LLM call
_STRUCTURED_OUTPUT_HINT = """
Return your answer as a JSON object:
{
  "blocks": [
    {"type": "text", "content": {"body": "Your prose answer here"}},
    {"type": "table", "content": {"title": "...", "columns": [{"name": "Col1", "type": "text"}], "rows": [["val1"]]}},
    {"type": "entity_card", "content": {"entityType": "...", "entityId": "...", "displayId": "...", "title": "...", "severity": "HIGH"}},
    {"type": "metric_row", "content": {"metrics": [{"label": "Total", "value": 42, "format": "number", "color": "red"}]}},
    {"type": "list", "content": {"title": "Actions", "style": "numbered", "items": [{"text": "Do this", "severity": "HIGH"}]}},
    {"type": "graph", "content": {"title": "...", "layout": "hierarchical", "nodes": [...], "edges": [...]}},
    {"type": "action_buttons", "content": {"actions": [{"label": "View", "action": "navigate", "params": {"path": "/..."}, "style": "primary"}]}}
  ],
  "payload": { ... },
  "followUps": ["Suggested question 1?", "Suggested question 2?"],
  "confidence": 0.95
}

Rules:
- Use "text" blocks for prose, "table" for tabular data, "entity_card" for clickable summaries
- Use "metric_row" for stats, "list" for recommendations, "graph" for relationship visualizations
- Use "action_buttons" for user actions (navigate, create, assign)
- NEVER include raw SQL, Cypher, or API queries in text blocks
- NEVER use emojis — strictly prohibited, no exceptions
- Include 3-5 followUp questions you can actually answer with your available tools
- Return ONLY valid JSON, no markdown fences
- For "entity_card" blocks: "entityId" MUST be an id that actually appeared in
  a tool result above — never invent one. Its "title" and other fields are
  looked up from the real record and replace whatever you write here, so
  focus on picking the right entityId, not on describing it.
"""


def _humanize_key(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()


class AgentRuntime:
    """Generic ReAct loop executor for specialist agents.

    Args:
        agent_name: Name of the specialist agent.
        tool_registry: Pre-built ToolRegistry with allowed tools.
        llm_router: LLMRouter configured with the specialist's lane.
        system_prompt: Pre-rendered system prompt for the specialist.
        step_emitter: For real-time SSE step events.
        max_iterations: Max ReAct loop iterations.
        max_llm_calls: Max total LLM calls (including tool-decision calls).
    """

    def __init__(
        self,
        agent_name: str,
        tool_registry: ToolRegistry,
        llm_router: LLMRouter,
        system_prompt: str,
        step_emitter: StepEmitter | None = None,
        max_iterations: int = 5,
        max_llm_calls: int = 8,
    ) -> None:
        self._name = agent_name
        self._registry = tool_registry
        self._router = llm_router
        self._system_prompt = system_prompt
        self._emitter = step_emitter or NullStepEmitter()
        self._max_iterations = max_iterations
        self._max_calls = max_llm_calls

    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> StructuredResponse:
        """Execute the ReAct loop and return structured response."""
        start_time = time.monotonic()
        history = history or []
        context = context or {}

        tools = self._registry.openai_tool_definitions()
        tools_used: list[str] = []
        sources: list[SourceRef] = []
        thinking: list[str] = []
        total_tokens = 0
        total_cost = 0.0
        # Real records seen from tool results this turn, keyed by id — the
        # only source of truth entity_card facts are allowed to come from
        # (see _ground_entity_cards).
        known_entities: dict[str, dict[str, Any]] = {}

        logger.info("[%s] START query=%r tools=%d", self._name, user_message[:120], len(tools))
        trace_logger.info(
            "[%s] START query=%r available_tools=%s",
            self._name, user_message[:120], self._registry.tool_names(),
        )

        # Build messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]
        for msg in history[-10:]:
            role = msg.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user_message})

        # ── ReAct Loop ────────────────────────────────────────────────────
        calls_made = 0
        tool_data: list[dict[str, Any]] = []

        for iteration in range(self._max_iterations):
            if calls_made >= self._max_calls:
                thinking.append("Reached tool call budget — preparing response")
                break

            try:
                # Force tool usage on first 2 iterations — LLM must gather data
                # before answering. After that, "auto" lets it respond when ready.
                tool_choice = "required" if iteration < 2 and tools else "auto"

                logger.info("[%s] iter=%d tool_choice=%s", self._name, iteration, tool_choice)
                result = await self._router.call(
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice=tool_choice if tools else None,
                    trace_name=f"{self._name}_iter_{iteration}",
                )
                calls_made += 1
                total_tokens += (result.tokens_input or 0) + (result.tokens_output or 0)
                total_cost += result.cost_usd
            except Exception as exc:
                logger.warning("[%s] LLM call failed: %s", self._name, exc)
                thinking.append("Encountered an issue — preparing response with available data")
                break

            # No tool calls → LLM wants to respond
            if not result.tool_calls:
                logger.info("[%s] iter=%d LLM chose to respond (no tool calls)", self._name, iteration)
                trace_logger.info("[%s] iter=%d -> respond directly (no further tool calls)", self._name, iteration)
                break

            # Execute tool calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": result.tool_calls,
            }
            messages.append(assistant_msg)

            tool_names_this_iter = [tc.get("function", {}).get("name", "?") for tc in result.tool_calls]
            logger.info("[%s] iter=%d LLM called tools: %s", self._name, iteration, tool_names_this_iter)
            trace_logger.info("[%s] iter=%d LLM selected tool(s): %s", self._name, iteration, tool_names_this_iter)

            for tc in result.tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                tool_args = tc.get("function", {}).get("arguments", "{}")
                friendly_name = tool_name.replace("fetch_", "").replace("_", " ")

                logger.info("[%s] TOOL_CALL %s args=%s", self._name, tool_name, tool_args[:200])

                await self._emitter.emit(
                    self._name, "in_progress",
                    f"Querying {friendly_name} data...",
                )
                thinking.append(f"Searching {friendly_name}...")

                tool_result = await self._registry.execute(tc)
                tools_used.append(tool_name)

                # Count records for source attribution, and index any
                # id-bearing records so entity_card blocks can be grounded
                # against them later (see _ground_entity_cards).
                record_count = 0
                if tool_result.success:
                    try:
                        parsed = json.loads(tool_result.content)
                        record_count = len(parsed) if isinstance(parsed, list) else 1
                        for record in (parsed if isinstance(parsed, list) else [parsed]):
                            if isinstance(record, dict) and record.get("id") is not None:
                                known_entities[str(record["id"])] = record
                    except (json.JSONDecodeError, TypeError):
                        record_count = 1

                sources.append(SourceRef(
                    tool=tool_name,
                    description=f"{'Graph' if 'fetch_' in tool_name else 'Database'} query: {friendly_name}",
                    recordCount=record_count,
                ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_result.tool_call_id,
                    "content": tool_result.content[:_MAX_TOOL_RESULT],
                })

                if tool_result.success and record_count > 0:
                    logger.info("[%s] TOOL_OK %s records=%d size=%d",
                                self._name, tool_name, record_count, len(tool_result.content))
                    thinking.append(f"Found {record_count} result(s) from {friendly_name}")
                elif not tool_result.success:
                    logger.warning("[%s] TOOL_FAIL %s error=%s",
                                   self._name, tool_name, tool_result.content[:200])
                    thinking.append(f"Could not retrieve {friendly_name} data")

                tool_data.append({
                    "tool": tool_name,
                    "result": tool_result.content[:2000],
                    "success": tool_result.success,
                })

        # ── Final structured output call ──────────────────────────────────
        if tools_used:
            await self._emitter.emit(self._name, "in_progress", "Analyzing findings...")
            thinking.append("Analyzing findings...")

        # Append structured output instruction as a user message.
        messages.append({
            "role": "user",
            "content": _STRUCTURED_OUTPUT_HINT,
        })

        try:
            # Budget-aware: never let the parse-retry loop push total calls
            # past self._max_calls, it just gets fewer retries if the ReAct
            # loop already spent most of the budget on tool calls.
            remaining_calls = max(1, self._max_calls - calls_made)
            max_attempts = min(_MAX_STRUCTURED_OUTPUT_RETRIES, remaining_calls)

            response, attempt_results = await self._call_for_structured_response(
                messages, max_attempts=max_attempts,
            )
            for r in attempt_results:
                calls_made += 1
                total_tokens += (r.tokens_input or 0) + (r.tokens_output or 0)
                total_cost += r.cost_usd

            final_raw = attempt_results[-1].content
            logger.info(
                "[%s] FINAL_LLM raw_length=%d attempts=%d", self._name, len(final_raw), len(attempt_results)
            )
            logger.debug("[%s] FINAL_LLM raw=%s", self._name, final_raw[:500])
            logger.info("[%s] PARSED responseType=%s blocks=%d payload=%s",
                        self._name, response.responseType, len(response.blocks),
                        "present" if response.payload else "null")
        except Exception as exc:
            logger.warning("[%s] final LLM call failed: %s", self._name, exc)
            response = simple_response(self._name, "I encountered an issue preparing the results. Please try again.")
            response.status = "error"

        # Ground entity_card facts in real tool data; drop any card
        # referencing an id the LLM invented (anti-hallucination).
        self._ground_entity_cards(response, known_entities)

        # ── Deterministic responseType from tools_used (via domain) ────────
        response.responseType = _infer_response_type(
            tools_used, response.responseType
        )

        # ── Populate envelope ─────────────────────────────────────────────
        duration_ms = int((time.monotonic() - start_time) * 1000)

        response.agent = self._name
        response.thinking = thinking
        response.sources = sources
        response.meta = ResponseMeta(
            toolsUsed=list(set(tools_used)),
            iterations=calls_made,
            tokensUsed=total_tokens,
            costUsd=round(total_cost, 6),
            durationMs=duration_ms,
            modelUsed=getattr(self._router, '_default_lane', ''),
            userId=context.get("user_id"),
            tenantId=context.get("tenant_id"),
            conversationId=context.get("conversation_id"),
        )

        # Validate follow-ups against available tool capabilities
        if response.followUps:
            response.followUps = _validate_followups(
                response.followUps, self._registry.tool_names()
            )

        await self._emitter.emit(
            self._name, "completed",
            f"Done — {len(response.blocks)} section(s) ready",
        )

        logger.info(
            "[%s] DONE status=%s responseType=%s blocks=%d tools=%s "
            "tokens=%d cost=$%.4f duration=%dms",
            self._name, response.status, response.responseType,
            len(response.blocks), tools_used,
            total_tokens, total_cost, duration_ms,
        )

        return response

    async def _call_for_structured_response(
        self, messages: list[dict[str, Any]], max_attempts: int,
    ) -> tuple[StructuredResponse, list[Any]]:
        """Call the LLM for the final structured answer, retrying on malformed
        JSON up to ``max_attempts`` times before falling back to a plain text
        block. Returns (response, call_results) — callers fold every attempt's
        tokens/cost into their own running totals, since each retry is a real
        billed LLM call.
        """
        call_results: list[Any] = []

        for attempt in range(max_attempts):
            result = await self._router.call(
                messages=messages, trace_name=f"{self._name}_final_attempt_{attempt + 1}",
            )
            call_results.append(result)

            parsed = self._try_parse_structured_response(result.content)
            if parsed is not None:
                return parsed, call_results

            logger.warning(
                "[%s] structured output parse failed (attempt %d/%d)",
                self._name, attempt + 1, max_attempts,
            )
            if attempt < max_attempts - 1:
                messages.append({"role": "assistant", "content": result.content})
                messages.append({
                    "role": "user",
                    "content": "That response was not valid JSON matching the schema above. "
                               "Return ONLY the JSON object — no markdown fences, no explanation.",
                })

        logger.debug("%s: structured parse failed after %d attempt(s), falling back to text", self._name, max_attempts)
        return (
            StructuredResponse(blocks=[text_block(call_results[-1].content)], status="partial"),
            call_results,
        )

    def _try_parse_structured_response(self, raw_content: str) -> StructuredResponse | None:
        """Attempt to parse LLM output into StructuredResponse. Returns None
        (no fallback) so the caller can decide whether to retry."""
        content = raw_content.strip()

        # Strip markdown fences
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(content)

            if isinstance(data, dict):
                if "blocks" in data:
                    parsed_blocks = []
                    for b in data["blocks"]:
                        block = self._parse_block(b)
                        if block:
                            parsed_blocks.append(block)
                        else:
                            logger.debug("%s: dropped malformed block: %s", self._name, str(b)[:100])
                    raw_rt = data.get("responseType", "text_only")
                    rt = raw_rt if raw_rt in _VALID_RESPONSE_TYPES else "text_only"

                    return StructuredResponse(
                        blocks=parsed_blocks,
                        responseType=rt,
                        payload=data.get("payload"),
                        followUps=data.get("followUps", []),
                        confidence=data.get("confidence", 0.9),
                    )
                if "type" in data and "content" in data:
                    block = self._parse_block(data)
                    if block:
                        return StructuredResponse(blocks=[block])

        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            logger.debug("%s: structured parse failed (%s)", self._name, exc)

        return None

    def _ground_entity_cards(
        self, response: StructuredResponse, known_entities: dict[str, dict[str, Any]],
    ) -> None:
        """Replace LLM-authored entity_card facts with the real tool record,
        and drop any card referencing an entityId that was never actually
        returned by a tool call this turn.

        The model gets to choose *which* entity to feature; it never gets to
        author what that entity's facts are — those come only from
        ``known_entities`` (collected in ``run()`` from actual tool results).
        Mutates ``response.blocks`` in place.
        """
        if not known_entities:
            # Nothing to ground against — an entity_card here can only be
            # hallucinated, since no tool returned any id-bearing record.
            grounded = [b for b in response.blocks if b.type != "entity_card"]
            if len(grounded) != len(response.blocks):
                logger.warning(
                    "[%s] dropped %d entity_card block(s) — no tool results to ground against",
                    self._name, len(response.blocks) - len(grounded),
                )
            response.blocks = grounded
            return

        grounded_blocks: list[ContentBlock] = []
        for block in response.blocks:
            if block.type != "entity_card" or not isinstance(block.content, EntityCardContent):
                grounded_blocks.append(block)
                continue

            record = known_entities.get(block.content.entityId)
            if record is None:
                logger.warning(
                    "[%s] dropped entity_card referencing unknown entityId=%r (not in any tool result)",
                    self._name, block.content.entityId,
                )
                continue

            block.content.title = str(record.get("name") or record.get("title") or record.get("id"))
            block.content.fields = [
                EntityField(label=_humanize_key(k), value=str(v))
                for k, v in record.items()
                if k not in _ENTITY_CARD_EXCLUDED_KEYS and v is not None
            ][:_MAX_ENTITY_CARD_FIELDS]
            grounded_blocks.append(block)

        response.blocks = grounded_blocks

    def _parse_block(self, block_data: dict) -> Any:
        """Parse a single block dict. Returns ContentBlock or None."""
        try:
            return ContentBlock.model_validate(block_data)
        except Exception:
            if "content" in block_data and isinstance(block_data["content"], dict):
                body = block_data["content"].get("body", "")
                if body:
                    return ContentBlock(
                        type="text",
                        content={"body": body},
                    )
            return None


# ── Deterministic responseType from tools_used (via domain) ─────────────────────


def _infer_response_type(tools_used: list[str], llm_response_type: str) -> str:
    """Deterministically set responseType from tools_used.

    Delegates to the domain's ``ToolMappingProvider.tool_to_response_type()``
    for each tool. First recognized tool determines the type.
    Falls back to the LLM's declared responseType if no tool matches.
    """
    if not tools_used:
        return llm_response_type

    tool_mapping = DomainRegistry.tool_mapping()

    for tool in tools_used:
        rt = tool_mapping.tool_to_response_type(tool)
        if rt:
            if rt != llm_response_type:
                logger.debug("responseType override: %s → %s (from tool %s)", llm_response_type, rt, tool)
            return rt

    return llm_response_type


# ── Follow-up question validation (via domain) ───────────────────────────────────


def _validate_followups(
    followups: list[str],
    available_tools: list[str],
) -> list[str]:
    """Filter follow-up questions to only those answerable by available tools.

    Delegates keyword matching to the domain's ``ToolMappingProvider``.
    """
    if not followups or not available_tools:
        return followups

    tool_mapping = DomainRegistry.tool_mapping()

    # Build keyword set from available tools (domain-specific)
    tool_keywords: set[str] = set()
    for tool in available_tools:
        for kw in tool_mapping.tool_capability_keywords(tool):
            tool_keywords.add(kw.lower())

    universal_keywords = [kw.lower() for kw in tool_mapping.universal_keywords]
    domain_anchors = {a.lower() for a in tool_mapping.domain_anchors}

    validated: list[str] = []
    for question in followups:
        q_lower = question.lower()
        has_tool_match = any(kw in q_lower for kw in tool_keywords)
        has_domain_match = (
            any(kw in q_lower for kw in universal_keywords)
            and any(anchor in q_lower for anchor in domain_anchors)
        )
        if has_tool_match or has_domain_match:
            validated.append(question)
        else:
            logger.debug("Dropped unanswerable follow-up: %s", question[:80])

    if not validated and followups:
        return followups[:2]

    return validated[:5]  # Cap at 5