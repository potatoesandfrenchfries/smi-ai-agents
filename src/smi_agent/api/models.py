"""API request/response models for the conversation interface."""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

_SESSION_ID_RE = re.compile(r"^[a-f0-9]{1,64}$")
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _new_session_id() -> str:
    return uuid.uuid4().hex


class PageContextRequest(BaseModel):
    page_type: str
    entity_id: str | None = None


class ChatRequest(BaseModel):
    session_id: str = Field(default_factory=_new_session_id)
    user_message: str = Field(min_length=1, max_length=4096)
    agent_name: str = "uc02_conversation"
    page_context: PageContextRequest | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str) -> str:
        if not _SESSION_ID_RE.match(v):
            raise ValueError("session_id must be 1–64 lowercase hex characters")
        return v

    @field_validator("agent_name")
    @classmethod
    def _validate_agent_name(cls, v: str) -> str:
        if not _AGENT_NAME_RE.match(v):
            raise ValueError(
                "agent_name must contain only alphanumeric, underscore, or hyphen characters (max 64)"
            )
        return v


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[dict[str, Any]]
    meta: dict[str, Any]


# ── Conversation models ────────────────────────────────────────────────────────

_CONTEXT_TYPES = {"entity", "list", "dashboard", "general"}  # Base set; domain extends at runtime
_CONV_STATUS = {"ACTIVE", "CEILING_HIT", "CLOSED", "ARCHIVED"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class ConversationContext(BaseModel):
    type: str
    entityId: str | None = None
    label: str | None = Field(default=None, max_length=200)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in _CONTEXT_TYPES:
            raise ValueError(f"Invalid context.type: {v}. Must be one of: {sorted(_CONTEXT_TYPES)}")
        return v

    @field_validator("entityId")
    @classmethod
    def _validate_entity_id(cls, v: str | None) -> str | None:
        if v is not None and not _UUID_RE.match(v):
            raise ValueError("context.entityId must be a valid UUID")
        return v


class CreateConversationRequest(BaseModel):
    agentName: str = "uc02_conversation"
    context: ConversationContext | None = None

    @field_validator("agentName")
    @classmethod
    def _validate_agent(cls, v: str) -> str:
        if not _AGENT_NAME_RE.match(v):
            raise ValueError("agentName must be alphanumeric/underscore/hyphen (max 64)")
        return v


class ConversationChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    pageContext: PageContextRequest | None = None


class ConversationCeiling(BaseModel):
    maxMessages: int
    maxTokens: int
    messagesUsed: int
    tokensUsed: int
    messagesRemaining: int
    tokensRemaining: int
    percentUsed: float
    status: str = "ok"
    statusMessage: str | None = None
    hitAt: str | None = None
    hitReason: str | None = None


class ConversationLastMessage(BaseModel):
    role: str
    preview: str
    at: str


class ConversationListItem(BaseModel):
    id: str
    displayId: str
    title: str | None
    status: str
    agentName: str
    context: ConversationContext | None
    ceiling: ConversationCeiling
    lastMessage: ConversationLastMessage | None
    messageCount: int
    createdAt: str
    updatedAt: str


class ConversationDetailResponse(BaseModel):
    id: str
    displayId: str
    title: str | None
    status: str
    agentName: str
    context: ConversationContext | None
    ceiling: ConversationCeiling
    compactionCount: int
    createdAt: str
    updatedAt: str
    closedAt: str | None


class ConversationListResponse(BaseModel):
    data: list[ConversationListItem]
    pagination: dict[str, Any]


class ConversationMessageItem(BaseModel):
    id: str
    seq: int
    role: str
    content: str
    tokens: int | None
    createdAt: str


class ConversationMessagesResponse(BaseModel):
    data: list[ConversationMessageItem]
    pagination: dict[str, Any]


class JsonPatchOp(BaseModel):
    op: str
    path: str
    value: Any = None

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str) -> str:
        if v not in ("replace", "test"):
            raise ValueError(f"Unsupported operation: {v}. Only replace and test are allowed.")
        return v


# ── Investigation models ───────────────────────────────────────────────────────


class CreateInvestigationRequest(BaseModel):
    """Request to create an investigation for a domain entity."""

    entity_id: str = Field(min_length=1)
    entity_type: str = "entity"
    tenant_id: str = Field(min_length=1)
    origin: str = "MANUAL"
    assignee: dict[str, Any] | None = None  # {"id": str, "name": str} or null
    config_name: str = "investigation_plan"

    @field_validator("config_name")
    @classmethod
    def _validate_config_name(cls, v: str) -> str:
        if not _AGENT_NAME_RE.match(v):
            raise ValueError("config_name must be alphanumeric/underscore/hyphen (max 64)")
        return v
