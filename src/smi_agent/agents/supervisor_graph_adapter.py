"""Adapter so SupervisorAgent can be driven through the same conversation
turn pipeline (persistence, ceiling tracking, SSE) that LangGraph
conversation graphs use.

conversation_runner.run_conversation_turn()'s only interaction with the
`graph` it's given is ``await graph.ainvoke(state, config=...)`` returning a
dict with ``assistant_reply``/``tokens_this_turn``/``history``/``errors``.
This class presents that same interface backed by SupervisorAgent.handle()
instead of a compiled StateGraph, so the multi-agent supervisor + specialist
system can serve /api/v1/conversations/{id}/chat without duplicating (or
bypassing) message persistence, ceiling enforcement, or session dual-write.
"""

from __future__ import annotations

import json
from typing import Any

from smi_agent.agents.response import StructuredResponse
from smi_agent.agents.supervisor import SupervisorAgent
from smi_agent.streaming import NullStepEmitter

# Tag persisted structured-message JSON so the frontend can tell it apart from
# ordinary plain-text content in the same Postgres column (conversation_messages
# .content is a single TEXT field shared by every agent, most of which write
# plain prose) — see web/src/api/structuredContent.ts for the reader side.
_STRUCTURED_CONTENT_KIND = "smi_structured_message_v1"


class SupervisorGraphAdapter:
    """Presents SupervisorAgent as a graph.ainvoke()-compatible callable."""

    def __init__(self, supervisor: SupervisorAgent) -> None:
        self._supervisor = supervisor

    async def ainvoke(
        self, state: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        page_context = state.get("page_context")
        context = {
            "user_id": state.get("user_id", ""),
            "tenant_id": state.get("tenant_id") or "00000000-0000-0000-0000-000000000001",
            "entity_id": (page_context.entity_id if page_context else "") or "",
            "conversation_id": state.get("session_id"),
        }
        step_emitter = state.get("_step_emitter") or NullStepEmitter()
        history: list[dict[str, Any]] = list(state.get("history") or [])

        response = await self._supervisor.handle(
            user_message=state["user_message"],
            history=history,
            context=context,
            step_emitter=step_emitter,
        )

        tokens_this_turn = response.meta.tokensUsed if response.meta else 0

        # Two different textual forms of the same response, for two different
        # readers:
        #   - summary_text: plain prose, becomes THIS turn's assistant entry in
        #     `history` — what the LLM itself reads back on the *next* turn as
        #     conversation context. A JSON blob there would waste tokens and
        #     read as noise to the model.
        #   - persisted_content: JSON-tagged blocks, becomes the Postgres
        #     `content` column — what a page reload or conversation reselect
        #     reads back to re-render real tables/metrics/lists, instead of
        #     falling back to flattened "[table]"-style placeholder text.
        summary_text = _flatten_blocks(response)
        persisted_content = _serialize_for_persistence(response)

        new_history = [
            *history,
            {"role": "user", "content": state["user_message"], "tokens": 0},
            {"role": "assistant", "content": summary_text, "tokens": tokens_this_turn},
        ]

        return {
            "assistant_reply": persisted_content,
            "tokens_this_turn": tokens_this_turn,
            "history": new_history,
            "errors": [],
            # Not a LangGraph field — run_conversation_turn publishes this via
            # SSEStreamer.publish_response so the frontend renders the rich
            # block/table/entity-card output instead of only plain text.
            "structured_response": response,
        }


def _flatten_blocks(response: StructuredResponse) -> str:
    """Plain-text summary of the response for LLM conversation history (see
    `history` in ainvoke() above) — never shown to the user directly. Text
    blocks are joined as-is; other block types (table, entity_card, ...) get
    a short placeholder so the model still knows *that* data was returned,
    without spending tokens re-reading a whole table back to itself.
    """
    parts: list[str] = []
    for block in response.blocks:
        if block.type == "text":
            body = getattr(block.content, "body", "") or ""
            if body:
                parts.append(body)
        else:
            parts.append(f"[{block.type}]")
    return "\n\n".join(parts)


def _serialize_for_persistence(response: StructuredResponse) -> str:
    """JSON-encode the full block list for Postgres persistence, so a later
    reload can re-render the actual tables/metrics/lists the user saw live,
    not just the flattened placeholder text `_flatten_blocks` produces.
    """
    return json.dumps({
        "kind": _STRUCTURED_CONTENT_KIND,
        "blocks": [b.model_dump(mode="json") for b in response.blocks],
    })
