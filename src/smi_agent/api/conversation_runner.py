"""Core conversation turn execution logic.

Dual-write architecture:
- Redis: hot cache for LLM context window (7-day TTL)
- Postgres: durable store for full transcript + ceiling tracking

On each turn:
1. Load from Redis (fast path) — if miss, restore from Postgres
2. Check ceiling — reject if exceeded
3. Run conversation graph
4. Dual-write: Redis (agent context) + Postgres (full transcript)
5. Update ceiling counters, emit warnings if thresholds crossed
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from smi_agent.conversation.schemas import SessionMeta
from smi_agent.conversation.session_store import SessionStore
from smi_agent.conversation.sse import SSEStreamer
from smi_agent.streaming import StepEmitter

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from smi_agent.postgres_client.safe_executor import SafePostgresExecutor

logger = logging.getLogger(__name__)


async def run_conversation_turn(
    state: dict[str, Any],
    graph: CompiledStateGraph,
    session_store: SessionStore,
    sse_streamer: SSEStreamer,
    session_meta: Any,
    *,
    pg_executor: SafePostgresExecutor | None = None,
    conversation_id: str | None = None,
    conversation_row: dict[str, Any] | None = None,
    ceiling_warning_pct: float = 80.0,
    ceiling_critical_pct: float = 90.0,
) -> None:
    """Execute one conversation turn with optional Postgres dual-write.

    Args:
        state: ConversationState dict for the graph.
        graph: Compiled conversation LangGraph.
        session_store: Redis session store.
        sse_streamer: SSE publisher.
        session_meta: SessionMeta for this session.
        pg_executor: Optional Postgres executor for dual-write.
        conversation_id: Postgres conversation UUID (if persisting).
        conversation_row: Current conversation row from Postgres (for ceiling checks).
        ceiling_warning_pct: Threshold for warning events.
        ceiling_critical_pct: Threshold for critical warning events.
    """
    session_id: str = state["session_id"]
    try:
        step_emitter = StepEmitter(sse_streamer._r, session_id)
        config = {"configurable": {"thread_id": session_id}}

        # Inject into state — LangGraph 0.6+ strips unknown keys from config
        state["_sse_streamer"] = sse_streamer
        state["_step_emitter"] = step_emitter
        result = await graph.ainvoke(state, config=config)

        # Surface any node-level errors to the client before done
        for err in result.get("errors") or []:
            await sse_streamer.publish_error(session_id, err)

        # Publish final assistant reply (ensures client has complete text even
        # if some streaming tokens were missed due to pub/sub timing)
        assistant_reply = result.get("assistant_reply", "")
        if assistant_reply:
            await sse_streamer.publish_token(session_id, assistant_reply)

        # ── Postgres dual-write ───────────────────────────────────────────
        tokens_this_turn = result.get("tokens_this_turn") or 0
        ceiling_data = None

        if pg_executor and conversation_id and conversation_row:
            try:
                ceiling_data = await _persist_turn_to_postgres(
                    pg=pg_executor,
                    conversation_id=conversation_id,
                    conversation_row=conversation_row,
                    user_message=state["user_message"],
                    assistant_reply=assistant_reply,
                    tokens_this_turn=tokens_this_turn,
                    compaction_record=result.get("compaction_record"),
                    compact_summary=_extract_compact_summary(result.get("history", [])),
                    warning_pct=ceiling_warning_pct,
                    critical_pct=ceiling_critical_pct,
                )

                # Emit ceiling warning/hit events
                if ceiling_data:
                    status = ceiling_data["ceiling"]["status"]
                    if status == "exceeded":
                        payload = json.dumps({"type": "ceiling", "data": ceiling_data})
                        from smi_agent.config.redis_keys import sse_channel

                        await sse_streamer._r.publish(sse_channel(session_id), payload)
                    elif status in ("warning", "critical"):
                        level = "approaching_limit" if status == "warning" else "near_limit"
                        payload = json.dumps(
                            {"type": "warning", "data": {"level": level, **ceiling_data}}
                        )
                        from smi_agent.config.redis_keys import sse_channel

                        await sse_streamer._r.publish(sse_channel(session_id), payload)

            except Exception:
                logger.exception("Postgres dual-write failed for conversation %s", conversation_id)
                # Non-fatal: Redis write still happens below

        # ── Emit meta event (always, before done) ─────────────────────────
        meta_data: dict[str, Any] = {"tokensThisTurn": tokens_this_turn}
        if ceiling_data:
            meta_data["ceiling"] = ceiling_data["ceiling"]
        meta_payload = json.dumps({"type": "meta", "data": meta_data})
        from smi_agent.config.redis_keys import sse_channel

        await sse_streamer._r.publish(sse_channel(session_id), meta_payload)

        await sse_streamer.publish_done(session_id)

        # ── Redis write (always) ──────────────────────────────────────────
        updated_meta = SessionMeta(
            session_id=session_meta.session_id,
            agent_name=session_meta.agent_name,
            created_at=session_meta.created_at,
            last_active_at=datetime.datetime.now(datetime.UTC),
            ttl_seconds=session_meta.ttl_seconds,
            turn_count=session_meta.turn_count + 1,
            total_tokens_used=session_meta.total_tokens_used + tokens_this_turn,
            compaction_count=session_meta.compaction_count
            + (1 if result.get("compaction_record") else 0),
        )
        await session_store.save(updated_meta, result.get("history", []))

    except Exception:
        logger.exception("run_conversation_turn: failed for session %s", session_id)
        await sse_streamer.publish_error(
            session_id, "An internal error occurred. Please try again."
        )
        await sse_streamer.publish_done(session_id)
        await session_store.save(session_meta, state.get("history", []))


async def restore_session_from_postgres(
    pg_executor: SafePostgresExecutor,
    conversation_id: str,
    user_id: str,
    max_recent: int = 10,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Restore a Redis session from Postgres after TTL expiry.

    Returns (conversation_row, history) or (None, []) if not found.
    The history includes the compact summary (if any) + last N messages.
    """
    rows = await pg_executor.run("fetch_conversation", conversation_id, user_id)
    if not rows:
        return None, []

    conv = rows[0]
    history: list[dict[str, Any]] = []

    # Add compact summary as system message if exists
    if conv.get("compact_summary"):
        history.append(
            {
                "role": "system",
                "content": f"[COMPACTION SUMMARY]\n{conv['compact_summary']}",
                "message_id": "compaction",
                "tokens": conv.get("compact_summary_tokens"),
            }
        )

    # Fetch recent messages (newest first, then reverse for chronological order)
    recent = await pg_executor.run(
        "fetch_conversation_recent_messages",
        conversation_id,
        max_recent,
    )
    for msg in reversed(recent):
        if msg.get("is_summary"):
            continue  # Skip summaries, we already added compact_summary above
        history.append(
            {
                "role": msg["role"],
                "content": msg["content"],
                "tokens": msg.get("tokens"),
            }
        )

    return conv, history


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _persist_turn_to_postgres(
    *,
    pg: SafePostgresExecutor,
    conversation_id: str,
    conversation_row: dict[str, Any],
    user_message: str,
    assistant_reply: str,
    tokens_this_turn: int,
    compaction_record: Any | None,
    compact_summary: str | None,
    warning_pct: float,
    critical_pct: float,
) -> dict[str, Any] | None:
    """Persist user + assistant messages and update counters atomically.

    Uses atomic SQL (message_count = message_count + N) with RETURNING
    to avoid race conditions from concurrent /chat calls on the same
    conversation. Seq numbers are derived from MAX(seq) at insert time.

    Returns ceiling data dict if a threshold was crossed, else None.
    """
    # ── All DB operations in a SINGLE transaction ───────────────────────
    # This prevents race conditions: seq assignment + message inserts +
    # counter update all happen atomically on the same connection.
    from smi_agent.api.conversation_service import compute_ceiling_status

    compaction_delta = 1 if compaction_record else 0
    compact_summary_at = None
    compact_summary_tokens = None
    if compaction_record:
        compact_summary_at = datetime.datetime.now(datetime.UTC)
        compact_summary_tokens = getattr(compaction_record, "summary_tokens", None)

    # Auto-title on first turn
    is_first_turn = conversation_row.get("message_count", 0) == 0
    title = None
    if is_first_turn:
        title = user_message[:100].strip()
        if len(user_message) > 100:
            title += "..."

    # Pre-compute ceiling status (estimate — real values come from RETURNING)
    max_msg = conversation_row.get("ceiling_max_messages", 100)
    max_tok = conversation_row.get("ceiling_max_tokens", 200000)
    est_msg = conversation_row.get("message_count", 0) + 2
    est_tok = conversation_row.get("token_count", 0) + tokens_this_turn
    ceiling_status, status_msg = compute_ceiling_status(
        est_msg,
        est_tok,
        max_msg,
        max_tok,
        warning_pct,
        critical_pct,
    )

    ceiling_hit_at = None
    ceiling_hit_reason = None
    new_status = None
    if ceiling_status == "exceeded":
        ceiling_hit_at = datetime.datetime.now(datetime.UTC)
        msg_pct = (est_msg / max_msg * 100) if max_msg > 0 else 0
        ceiling_hit_reason = "max_messages" if msg_pct >= 100 else "max_tokens"
        new_status = "CEILING_HIT"

    user_msg_id = str(uuid.uuid4())
    asst_msg_id = str(uuid.uuid4())

    # Build transaction: SELECT seq → INSERT user → INSERT assistant → UPDATE RETURNING
    # All on the same connection, serialized within the transaction.
    await pg.run_transaction_returning(
        [
            # 1. Get next seq (within this transaction, serialized)
            ("next_message_seq", (conversation_id,)),
            # 2-3. Insert messages (seq values are set below after we restructure)
            #   ... we need seq from step 1, but run_transaction_returning executes sequentially.
            #   Since we can't read intermediate results, use a different approach:
            #   Insert with subquery-based seq directly.
        ]
    )

    # Actually, run_transaction_returning doesn't let us read intermediate results.
    # Use the client's raw transaction method instead for full control:
    pool = await pg._client._ensure_pool()
    async with pool.acquire() as conn, conn.transaction():
        # 1. Atomic seq — SELECT FOR UPDATE style (serialized within txn)
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM conversation_messages WHERE conversation_id = $1",
            conversation_id,
        )
        next_seq = row["next_seq"] if row else 1

        # 2. Insert user message
        await conn.execute(
            "INSERT INTO conversation_messages "
            "(id, conversation_id, seq, role, content, tokens, is_summary, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
            uuid.UUID(user_msg_id),
            conversation_id,
            next_seq,
            "user",
            user_message,
            None,
            False,
        )

        # 3. Insert assistant message
        await conn.execute(
            "INSERT INTO conversation_messages "
            "(id, conversation_id, seq, role, content, tokens, is_summary, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
            uuid.UUID(asst_msg_id),
            conversation_id,
            next_seq + 1,
            "assistant",
            assistant_reply,
            tokens_this_turn,
            False,
        )

        # 4. Atomic counter update with RETURNING
        returning_row = await conn.fetchrow(
            "UPDATE conversations "
            "SET message_count = message_count + $2, "
            "    token_count = token_count + $3, "
            "    ceiling_status = $4, "
            "    title = COALESCE($5, title), "
            "    status = COALESCE($6, status), "
            "    ceiling_hit_at = COALESCE($7, ceiling_hit_at), "
            "    ceiling_hit_reason = COALESCE($8, ceiling_hit_reason), "
            "    compact_summary = COALESCE($9, compact_summary), "
            "    compact_summary_at = COALESCE($10, compact_summary_at), "
            "    compact_summary_tokens = COALESCE($11, compact_summary_tokens), "
            "    compaction_count = compaction_count + $12, "
            "    updated_at = now(), "
            "    closed_at = COALESCE($13, closed_at) "
            "WHERE id = $1 "
            "RETURNING message_count, token_count, ceiling_max_messages, ceiling_max_tokens",
            conversation_id,
            2,
            tokens_this_turn,
            ceiling_status,
            title,
            new_status,
            ceiling_hit_at,
            ceiling_hit_reason,
            compact_summary,
            compact_summary_at,
            compact_summary_tokens,
            compaction_delta,
            None,
        )

    # ── Build ceiling response from RETURNING values ──────────────────────
    if returning_row:
        new_msg_count = returning_row["message_count"]
        new_tok_count = returning_row["token_count"]
        real_max_msg = returning_row["ceiling_max_messages"]
        real_max_tok = returning_row["ceiling_max_tokens"]
    else:
        new_msg_count = est_msg
        new_tok_count = est_tok
        real_max_msg = max_msg
        real_max_tok = max_tok

    # Recompute ceiling from real DB values (handles concurrent increments)
    real_ceiling_status, real_status_msg = compute_ceiling_status(
        new_msg_count,
        new_tok_count,
        real_max_msg,
        real_max_tok,
        warning_pct,
        critical_pct,
    )

    pct = max(
        (new_msg_count / real_max_msg * 100) if real_max_msg > 0 else 0,
        (new_tok_count / real_max_tok * 100) if real_max_tok > 0 else 0,
    )

    if real_ceiling_status != "ok":
        return {
            "ceiling": {
                "messagesUsed": new_msg_count,
                "maxMessages": real_max_msg,
                "tokensUsed": new_tok_count,
                "maxTokens": real_max_tok,
                "messagesRemaining": max(0, real_max_msg - new_msg_count),
                "tokensRemaining": max(0, real_max_tok - new_tok_count),
                "percentUsed": round(pct, 1),
                "status": real_ceiling_status,
                "statusMessage": real_status_msg,
            },
            "reason": ceiling_hit_reason,
        }
    return None


def _extract_compact_summary(history: list[dict[str, Any]]) -> str | None:
    """Extract the latest compaction summary from history if present."""
    for msg in history:
        if msg.get("role") == "system" and msg.get("content", "").startswith(
            "[COMPACTION SUMMARY]"
        ):
            return msg["content"].replace("[COMPACTION SUMMARY]\n", "", 1)
    return None
