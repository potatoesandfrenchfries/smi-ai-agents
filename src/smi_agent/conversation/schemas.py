"""Pydantic schemas for the conversation layer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=_utcnow)
    message_id: str = Field(default_factory=_new_id)
    tokens: int | None = None


class PageContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_type: str = "general"  # Domain-specific; validated at runtime via EntityResolver
    entity_id: str | None = None
    entity_snapshot: dict[str, Any] = Field(default_factory=dict)
    injected_at: datetime = Field(default_factory=_utcnow)


class SessionMeta(BaseModel):
    session_id: str
    agent_name: str
    created_at: datetime = Field(default_factory=_utcnow)
    last_active_at: datetime = Field(default_factory=_utcnow)
    ttl_seconds: int = 604800
    turn_count: int = 0
    total_tokens_used: int = 0
    compaction_count: int = 0


class CompactionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    compacted_at: datetime = Field(default_factory=_utcnow)
    turns_before: int
    turns_after: int
    summary_tokens: int
    original_tokens: int
