"""compact_node — summarise old history turns when context window is near-full."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from smi_agent.config.models import AgentDefinition
from smi_agent.conversation.schemas import CompactionRecord
from smi_agent.conversation.state import ConversationState
from smi_agent.conversation.token_counter import count_message_tokens_async
from smi_agent.llm.prompts import PromptLoader
from smi_agent.llm.router import LLMRouter
from smi_agent.streaming import get_step_emitter

logger = logging.getLogger(__name__)

_SUMMARY_PREFIX = "[COMPACTION SUMMARY]"


def make_compact_node(defn: AgentDefinition, prompt_loader: PromptLoader):
    _keep_turns = max(1, defn.conversation.max_history_turns // 4)
    _summary_lane = defn.conversation.summary_lane
    # Hoisted: one router instance per graph, not per turn
    _router = LLMRouter(lane=_summary_lane, temperature=0.1)

    async def compact_node(
        state: ConversationState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        config = config or {}
        emitter = get_step_emitter(config)
        errors: list[str] = list(state.get("errors") or [])
        history: list[dict[str, Any]] = list(state.get("history") or [])
        turns_before = len(history)

        if turns_before <= _keep_turns:
            return {
                "history": history,
                "compaction_record": None,
                "errors": errors,
                "current_node": "compact",
            }

        to_summarise = history[:-_keep_turns]
        to_keep = history[-_keep_turns:]

        await emitter.emit("compact", "in_progress", "Compacting conversation history...")

        try:
            user_text = prompt_loader.render_user(
                "conversation_summary",
                {"turns": to_summarise},
            )
            result = await _router.call(
                messages=[{"role": "user", "content": user_text}],
                trace_name="compaction_summary",
            )
            summary_content = f"{_SUMMARY_PREFIX}\n{result.content}"
            summary_msg: dict[str, Any] = {
                "role": "system",
                "content": summary_content,
                "message_id": "compaction",
                "tokens": result.tokens_output,
            }
            new_history = [summary_msg] + to_keep
            record = CompactionRecord(
                compacted_at=datetime.now(UTC),
                turns_before=turns_before,
                turns_after=len(new_history),
                summary_tokens=result.tokens_output,
                original_tokens=await count_message_tokens_async(to_summarise),
            )
            await emitter.emit(
                "compact",
                "completed",
                f"History compacted: {turns_before} -> {len(new_history)} turns",
                detail={"turns_before": turns_before, "turns_after": len(new_history)},
            )
            return {
                "history": new_history,
                "compaction_record": record,
                "context_token_count": await count_message_tokens_async(new_history),
                "errors": errors,
                "current_node": "compact",
            }
        except Exception as exc:
            err_msg = f"compact: summarisation failed: {type(exc).__name__}"
            logger.warning(err_msg)
            errors.append(err_msg)
            return {
                "history": history,
                "compaction_record": None,
                "errors": errors,
                "current_node": "compact",
            }

    return compact_node
