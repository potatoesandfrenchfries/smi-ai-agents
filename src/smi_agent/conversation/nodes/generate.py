"""generate_node — multi-turn LLM response generation with token streaming."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from smi_agent.agents.guardrails import sanitize_text
from smi_agent.config.models import AgentDefinition
from smi_agent.conversation.state import ConversationState
from smi_agent.conversation.token_counter import count_message_tokens_async, should_compact
from smi_agent.llm.prompts import PromptLoader
from smi_agent.llm.router import LLMRouter
from smi_agent.schemas.internal import BudgetExceededError
from smi_agent.streaming import get_step_emitter

logger = logging.getLogger(__name__)


def _get_sse_streamer(state: dict[str, Any]):
    """Extract (sse_streamer, session_id) from state, or (None, None)."""
    return state.get("_sse_streamer"), state.get("session_id")


def make_generate_node(defn: AgentDefinition, prompt_loader: PromptLoader):
    _lane = defn.llm.lane
    _temperature = defn.llm.temperature
    _threshold_pct = defn.conversation.compaction_threshold_pct
    _ctx_window = defn.context_window_tokens
    _router = LLMRouter(lane=_lane, temperature=_temperature)

    async def generate_node(
        state: ConversationState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        config = config or {}
        emitter = get_step_emitter(config, state)
        sse_streamer, session_id = _get_sse_streamer(state)
        errors: list[str] = list(state.get("errors") or [])
        history: list[dict[str, Any]] = list(state.get("history") or [])
        user_message: str = state["user_message"]
        page_context = state.get("page_context")

        await emitter.emit("generate", "in_progress", "Generating response...")

        # Build system prompt context
        page_type = page_context.page_type if page_context else "unknown"
        entity_id = (page_context.entity_id or "") if page_context else ""
        snapshot = (page_context.entity_snapshot or {}) if page_context else {}
        snapshot_summary = json.dumps(snapshot, default=str)[:500]

        # Include tool context from ReAct loop (if any)
        tool_context = state.get("tool_context") or []
        tool_data_block = ""
        if tool_context:
            tool_data_block = "\n\n## Data Retrieved by Tools\n"
            for tc in tool_context:
                name = tc.get("tool_name", "unknown")
                result = tc.get("result", "")
                if name == "_llm_summary":
                    continue  # Skip LLM's own summary
                tool_data_block += f"### {name}\n{result[:1500]}\n\n"

        system_block = prompt_loader.render_system(
            "system",
            {
                "agent_name": defn.name,
                "page_type": page_type,
                "entity_id": entity_id,
                "entity_snapshot_summary": snapshot_summary + tool_data_block,
            },
        )

        # Assemble messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [system_block]},
        ]
        for msg in history:
            role = msg.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": msg.get("content", "")})

        messages.append({"role": "user", "content": user_message})

        # Pre-flight compaction check
        all_tokens = await count_message_tokens_async(messages)
        needs_compact = should_compact(all_tokens, _ctx_window, _threshold_pct)

        try:
            # Use streaming when SSE channel is available
            if sse_streamer and session_id:
                assistant_content, tokens_in, tokens_out = await _stream_llm_response(
                    _router, messages, sse_streamer, session_id,
                )
            else:
                result = await _router.call(
                    messages=messages,
                    trace_name="conversation_generate",
                )
                assistant_content = result.content
                tokens_in = result.tokens_input or 0
                tokens_out = result.tokens_output or 0

            assistant_content = sanitize_text(assistant_content)
            tokens_this_turn = tokens_in + tokens_out

            history = history + [
                {"role": "user", "content": user_message, "tokens": tokens_in},
                {"role": "assistant", "content": assistant_content, "tokens": tokens_out},
            ]
            new_token_count = await count_message_tokens_async(history)

            return {
                "assistant_reply": assistant_content,
                "tokens_this_turn": tokens_this_turn,
                "history": history,
                "context_token_count": new_token_count,
                "should_compact": should_compact(new_token_count, _ctx_window, _threshold_pct),
                "errors": errors,
                "current_node": "generate",
            }

        except BudgetExceededError as exc:
            err_msg = f"generate: budget exceeded: {exc}"
            logger.warning(err_msg)
            errors.append(err_msg)
        except Exception as exc:
            err_msg = f"generate: LLM call failed: {type(exc).__name__}: {exc}"
            logger.warning(err_msg, exc_info=True)
            errors.append(f"generate: LLM call failed: {type(exc).__name__}")

        return {
            "assistant_reply": "",
            "tokens_this_turn": 0,
            "history": history,
            "context_token_count": await count_message_tokens_async(history),
            "should_compact": needs_compact,
            "errors": errors,
            "current_node": "generate",
        }

    return generate_node


async def _stream_llm_response(
    router: LLMRouter,
    messages: list[dict[str, Any]],
    sse_streamer: Any,
    session_id: str,
) -> tuple[str, int, int]:
    """Stream LLM response tokens to SSE channel as they arrive.

    Returns (full_text, tokens_in, tokens_out).
    """
    stream, _lane, _model = await router.stream_call(
        messages=messages,
        trace_name="conversation_generate",
    )

    full_text = ""
    tokens_in = 0
    tokens_out = 0

    async for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if choice and choice.delta and choice.delta.content:
            full_text += choice.delta.content
            # Publish accumulated text (frontend replaces the assistant bubble)
            await sse_streamer.publish_token(session_id, sanitize_text(full_text))

        # Extract usage from final chunk (Anthropic / OpenAI include it)
        if hasattr(chunk, "usage") and chunk.usage:
            tokens_in = getattr(chunk.usage, "prompt_tokens", 0) or 0
            tokens_out = getattr(chunk.usage, "completion_tokens", 0) or 0

    # Fallback: estimate tokens if provider didn't report usage
    if tokens_in == 0 and tokens_out == 0:
        from smi_agent.conversation.token_counter import count_message_tokens_async

        tokens_in = await count_message_tokens_async(messages)
        tokens_out = max(1, len(full_text) // 4)  # rough estimate

    return full_text, tokens_in, tokens_out
