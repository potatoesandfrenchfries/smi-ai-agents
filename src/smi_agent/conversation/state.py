"""LangGraph ConversationState TypedDict."""

from __future__ import annotations

from typing import Any, NotRequired

from typing_extensions import TypedDict

from smi_agent.conversation.schemas import CompactionRecord, PageContext


class ConversationState(TypedDict):
    # Identity
    session_id: str
    agent_name: str

    # Input for this turn
    user_message: str
    page_context: NotRequired[PageContext | None]

    # History (list of ChatMessage.model_dump(mode="json") for serialisability)
    history: list[dict[str, Any]]

    # Working fields
    context_token_count: NotRequired[int]
    should_compact: NotRequired[bool]

    # Output
    assistant_reply: NotRequired[str]
    tokens_this_turn: NotRequired[int]
    compaction_record: NotRequired[CompactionRecord | None]

    # Housekeeping
    errors: NotRequired[list[str]]
    current_node: NotRequired[str]

    # Tenant (for tool parameter injection)
    tenant_id: NotRequired[str]

    # ReAct tool-calling loop
    tool_context: NotRequired[list[dict[str, Any]]]
    tool_iterations: NotRequired[int]

    # Streaming (injected by conversation_runner, not serialized)
    _sse_streamer: NotRequired[Any]
    _step_emitter: NotRequired[Any]
