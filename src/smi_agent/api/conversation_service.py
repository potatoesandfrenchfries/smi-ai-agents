"""Conversation persistence service — Postgres CRUD for conversations and messages.

Handles:
- Conversation lifecycle (create, get, list, update, archive)
- Message persistence (dual-write alongside Redis)
- Ceiling tracking and threshold enforcement
- Redis restore from Postgres on TTL expiry
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from smi_agent.postgres_client.safe_executor import SafePostgresExecutor
from smi_agent.utils.display_id import uuid_to_display_id

logger = logging.getLogger(__name__)


# ── Ceiling helpers ────────────────────────────────────────────────────────────


def compute_ceiling_status(
    message_count: int,
    token_count: int,
    max_messages: int,
    max_tokens: int,
    warning_pct: float = 80.0,
    critical_pct: float = 90.0,
) -> tuple[str, str | None]:
    """Compute ceiling status and message.

    Returns (status, status_message) where status is one of:
    ok, warning, critical, exceeded.
    """
    msg_pct = (message_count / max_messages * 100) if max_messages > 0 else 0
    tok_pct = (token_count / max_tokens * 100) if max_tokens > 0 else 0
    pct = max(msg_pct, tok_pct)

    if pct >= 100:
        return "exceeded", "Conversation limit reached. Start a new conversation."
    if pct >= critical_pct:
        return "critical", "This conversation is nearly full. Consider starting a new one."
    if pct >= warning_pct:
        return "warning", "You're approaching the conversation limit."
    return "ok", None


def build_ceiling_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Build ceiling response dict from a conversation row."""
    max_msg = row.get("ceiling_max_messages", 100)
    max_tok = row.get("ceiling_max_tokens", 200000)
    msg_used = row.get("message_count", 0)
    tok_used = row.get("token_count", 0)
    pct = max(
        (msg_used / max_msg * 100) if max_msg > 0 else 0,
        (tok_used / max_tok * 100) if max_tok > 0 else 0,
    )
    return {
        "maxMessages": max_msg,
        "maxTokens": max_tok,
        "messagesUsed": msg_used,
        "tokensUsed": tok_used,
        "messagesRemaining": max(0, max_msg - msg_used),
        "tokensRemaining": max(0, max_tok - tok_used),
        "percentUsed": round(pct, 1),
        "status": row.get("ceiling_status", "ok"),
        "statusMessage": None,
        "hitAt": _iso(row.get("ceiling_hit_at")),
        "hitReason": row.get("ceiling_hit_reason"),
    }


# ── Service functions ──────────────────────────────────────────────────────────


async def create_conversation(
    pg: SafePostgresExecutor,
    *,
    tenant_id: str,
    user_id: str,
    agent_name: str,
    context: dict[str, Any] | None = None,
    ceiling_max_messages: int = 100,
    ceiling_max_tokens: int = 200000,
) -> dict[str, Any]:
    """Create a new conversation. Returns the conversation dict."""
    conv_id = str(uuid.uuid4())
    display_id = uuid_to_display_id(uuid.UUID(conv_id), "CONV")

    ctx_type = context.get("type") if context else None
    ctx_entity = context.get("entityId") if context else None
    ctx_label = context.get("label") if context else None

    await pg.execute(
        "insert_conversation",
        conv_id,
        tenant_id,
        user_id,
        display_id,
        agent_name,
        None,
        "ACTIVE",
        ctx_type,
        ctx_entity,
        ctx_label,
        ceiling_max_messages,
        ceiling_max_tokens,
    )

    return {
        "id": conv_id,
        "displayId": display_id,
        "status": "ACTIVE",
        "agentName": agent_name,
        "context": context,
        "ceiling": {
            "maxMessages": ceiling_max_messages,
            "maxTokens": ceiling_max_tokens,
            "messagesUsed": 0,
            "tokensUsed": 0,
            "messagesRemaining": ceiling_max_messages,
            "tokensRemaining": ceiling_max_tokens,
            "percentUsed": 0.0,
            "status": "ok",
        },
        "title": None,
        "createdAt": datetime.now(UTC).isoformat(),
        "updatedAt": datetime.now(UTC).isoformat(),
    }


async def get_conversation(
    pg: SafePostgresExecutor,
    conversation_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    """Fetch a conversation by ID, scoped to user. Returns None if not found."""
    rows = await pg.run("fetch_conversation", conversation_id, user_id)
    if not rows:
        return None
    row = rows[0]
    return _row_to_detail(row)


async def list_conversations(
    pg: SafePostgresExecutor,
    *,
    user_id: str,
    tenant_id: str,
    status: str | None = None,
    context_type: str | None = None,
    context_entity_id: str | None = None,
    context_label: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """List conversations with filters. Returns (items, total)."""
    rows = await pg.run(
        "list_conversations",
        user_id,
        tenant_id,
        status,
        context_type,
        context_entity_id,
        context_label,
        limit,
        offset,
    )
    count_rows = await pg.run(
        "count_conversations",
        user_id,
        tenant_id,
        status,
        context_type,
        context_entity_id,
        context_label,
    )
    total = count_rows[0]["total"] if count_rows else 0

    items = []
    for row in rows:
        item = _row_to_list_item(row)
        # Fetch last message preview
        preview_rows = await pg.run("fetch_last_message_preview", row["id"])
        if preview_rows:
            p = preview_rows[0]
            item["lastMessage"] = {
                "role": p["role"],
                "preview": p["preview"],
                "at": _iso(p["at"]),
            }
        else:
            item["lastMessage"] = None
        items.append(item)

    return items, total


async def get_messages(
    pg: SafePostgresExecutor,
    conversation_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Get messages for a conversation (excludes summaries). Returns (messages, total)."""
    rows = await pg.run("fetch_conversation_messages", conversation_id, limit, offset)
    count_rows = await pg.run("count_conversation_messages", conversation_id)
    total = count_rows[0]["total"] if count_rows else 0

    messages = [
        {
            "id": str(r["id"]),
            "seq": r["seq"],
            "role": r["role"],
            "content": r["content"],
            "tokens": r.get("tokens"),
            "createdAt": _iso(r["created_at"]),
        }
        for r in rows
    ]
    return messages, total


async def count_active_conversations(
    pg: SafePostgresExecutor,
    user_id: str,
    tenant_id: str,
) -> int:
    """Count active conversations for a user."""
    rows = await pg.run("count_active_conversations", user_id, tenant_id)
    return rows[0]["total"] if rows else 0


async def persist_message(
    pg: SafePostgresExecutor,
    *,
    conversation_id: str,
    seq: int,
    role: str,
    content: str,
    tokens: int | None = None,
    is_summary: bool = False,
) -> str:
    """Insert a message and return its ID."""
    msg_id = str(uuid.uuid4())
    await pg.execute(
        "insert_conversation_message",
        msg_id,
        conversation_id,
        seq,
        role,
        content,
        tokens,
        is_summary,
    )
    return msg_id


async def update_conversation_counters(
    pg: SafePostgresExecutor,
    *,
    conversation_id: str,
    message_count: int,
    token_count: int,
    ceiling_status: str,
    title: str | None = None,
    status: str | None = None,
    ceiling_hit_at: datetime | None = None,
    ceiling_hit_reason: str | None = None,
    compact_summary: str | None = None,
    compact_summary_at: datetime | None = None,
    compact_summary_tokens: int | None = None,
    compaction_count: int = 0,
    closed_at: datetime | None = None,
) -> None:
    """Update conversation counters, ceiling status, and optional fields."""
    await pg.execute(
        "update_conversation",
        conversation_id,
        message_count,
        token_count,
        ceiling_status,
        title,
        status,
        ceiling_hit_at,
        ceiling_hit_reason,
        compact_summary,
        compact_summary_at,
        compact_summary_tokens,
        compaction_count,
        closed_at,
    )


# ── Mapping helpers ────────────────────────────────────────────────────────────


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _row_to_detail(row: dict[str, Any]) -> dict[str, Any]:
    ctx = None
    if row.get("context_type"):
        ctx = {
            "type": row["context_type"],
            "entityId": row.get("context_entity_id"),
            "label": row.get("context_label"),
        }
    return {
        "id": str(row["id"]),
        "tenantId": str(row.get("tenant_id", "")),
        "displayId": row["display_id"],
        "title": row.get("title"),
        "status": row["status"],
        "agentName": row["agent_name"],
        "context": ctx,
        "ceiling": build_ceiling_dict(row),
        "compactionCount": row.get("compaction_count", 0),
        "templateContent": row.get("template_content"),
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
        "closedAt": _iso(row.get("closed_at")),
    }


def _row_to_list_item(row: dict[str, Any]) -> dict[str, Any]:
    ctx = None
    if row.get("context_type"):
        ctx = {
            "type": row["context_type"],
            "entityId": row.get("context_entity_id"),
            "label": row.get("context_label"),
        }
    return {
        "id": str(row["id"]),
        "displayId": row["display_id"],
        "title": row.get("title"),
        "status": row["status"],
        "agentName": row["agent_name"],
        "context": ctx,
        "ceiling": build_ceiling_dict(row),
        "messageCount": row.get("message_count", 0),
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
    }
