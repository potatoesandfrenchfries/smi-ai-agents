"""Canned SQL query registry — generic conversation + investigation queries.

Domain-specific queries are provided by the configured ``QueryProvider``
via ``DomainRegistry.query_provider()``.

Every query the agent may execute is registered here with a name, SQL text,
and an allowlist of agents permitted to run it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SqlQuery:
    """A registered canned SQL query."""

    name: str
    sql: str
    is_read: bool  # True = SELECT, False = INSERT/UPDATE
    allowed_agents: list[str] = field(default_factory=lambda: ["*"])


# ── Conversation queries (generic — same for every domain) ────────────────────

INSERT_CONVERSATION = SqlQuery(
    name="insert_conversation",
    sql="""\
INSERT INTO conversations (
    id, tenant_id, user_id, display_id, agent_name, title, status,
    context_type, context_entity_id, context_label,
    ceiling_max_messages, ceiling_max_tokens,
    created_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10,
    $11, $12,
    now(), now()
)
""",
    is_read=False,
)

FETCH_CONVERSATION = SqlQuery(
    name="fetch_conversation",
    sql="""\
SELECT id, tenant_id, user_id, display_id, agent_name, title, status,
       context_type, context_entity_id, context_label,
       message_count, token_count, ceiling_max_messages, ceiling_max_tokens,
       ceiling_status, ceiling_hit_at, ceiling_hit_reason,
       compaction_count, compact_summary, compact_summary_at, compact_summary_tokens,
       template_content,
       created_at, updated_at, closed_at
FROM conversations
WHERE id = $1 AND user_id = $2
""",
    is_read=True,
)

LIST_CONVERSATIONS = SqlQuery(
    name="list_conversations",
    sql="""\
SELECT c.id, c.display_id, c.title, c.status, c.agent_name,
       c.context_type, c.context_entity_id, c.context_label,
       c.message_count, c.token_count,
       c.ceiling_max_messages, c.ceiling_max_tokens, c.ceiling_status,
       c.created_at, c.updated_at
FROM conversations c
WHERE c.user_id = $1 AND c.tenant_id = $2
  AND ($3::varchar IS NULL OR c.status = $3)
  AND ($4::varchar IS NULL OR c.context_type = $4)
  AND ($5::varchar IS NULL OR c.context_entity_id = $5)
  AND ($6::varchar IS NULL OR c.context_label = $6)
ORDER BY c.updated_at DESC
LIMIT $7 OFFSET $8
""",
    is_read=True,
)

COUNT_CONVERSATIONS = SqlQuery(
    name="count_conversations",
    sql="""\
SELECT count(*) AS total
FROM conversations
WHERE user_id = $1 AND tenant_id = $2
  AND ($3::varchar IS NULL OR status = $3)
  AND ($4::varchar IS NULL OR context_type = $4)
  AND ($5::varchar IS NULL OR context_entity_id = $5)
  AND ($6::varchar IS NULL OR context_label = $6)
""",
    is_read=True,
)

COUNT_ACTIVE_CONVERSATIONS = SqlQuery(
    name="count_active_conversations",
    sql="""\
SELECT count(*) AS total
FROM conversations
WHERE user_id = $1 AND tenant_id = $2 AND status = 'ACTIVE'
""",
    is_read=True,
)

INSERT_CONVERSATION_MESSAGE = SqlQuery(
    name="insert_conversation_message",
    sql="""\
INSERT INTO conversation_messages (
    id, conversation_id, seq, role, content, tokens, is_summary, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, now())
""",
    is_read=False,
)

FETCH_CONVERSATION_MESSAGES = SqlQuery(
    name="fetch_conversation_messages",
    sql="""\
SELECT id, seq, role, content, tokens, created_at
FROM conversation_messages
WHERE conversation_id = $1 AND is_summary = FALSE
ORDER BY seq ASC
LIMIT $2 OFFSET $3
""",
    is_read=True,
)

COUNT_CONVERSATION_MESSAGES = SqlQuery(
    name="count_conversation_messages",
    sql="""\
SELECT count(*) AS total
FROM conversation_messages
WHERE conversation_id = $1 AND is_summary = FALSE
""",
    is_read=True,
)

FETCH_CONVERSATION_RECENT_MESSAGES = SqlQuery(
    name="fetch_conversation_recent_messages",
    sql="""\
SELECT id, seq, role, content, tokens, is_summary, created_at
FROM conversation_messages
WHERE conversation_id = $1
ORDER BY seq DESC
LIMIT $2
""",
    is_read=True,
)

UPDATE_CONVERSATION_ATOMIC = SqlQuery(
    name="update_conversation_atomic",
    sql="""\
UPDATE conversations
SET message_count = message_count + $2,
    token_count = token_count + $3,
    ceiling_status = $4,
    title = COALESCE($5, title),
    status = COALESCE($6, status),
    ceiling_hit_at = COALESCE($7, ceiling_hit_at),
    ceiling_hit_reason = COALESCE($8, ceiling_hit_reason),
    compact_summary = COALESCE($9, compact_summary),
    compact_summary_at = COALESCE($10, compact_summary_at),
    compact_summary_tokens = COALESCE($11, compact_summary_tokens),
    compaction_count = compaction_count + $12,
    updated_at = now(),
    closed_at = COALESCE($13, closed_at)
WHERE id = $1
RETURNING message_count, token_count, ceiling_max_messages, ceiling_max_tokens
""",
    is_read=False,
)

NEXT_MESSAGE_SEQ = SqlQuery(
    name="next_message_seq",
    sql="""\
SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
FROM conversation_messages
WHERE conversation_id = $1
""",
    is_read=True,
)

UPDATE_CONVERSATION_STATUS = SqlQuery(
    name="update_conversation_status",
    sql="""\
UPDATE conversations
SET status = $2::varchar, updated_at = now(),
    closed_at = CASE WHEN $2::varchar IN ('CLOSED', 'ARCHIVED') THEN now() ELSE closed_at END
WHERE id = $1 AND user_id = $3
""",
    is_read=False,
)

UPDATE_CONVERSATION_TITLE = SqlQuery(
    name="update_conversation_title",
    sql="""\
UPDATE conversations
SET title = $2, updated_at = now()
WHERE id = $1 AND user_id = $3
""",
    is_read=False,
)

FETCH_LAST_MESSAGE_PREVIEW = SqlQuery(
    name="fetch_last_message_preview",
    sql="""\
SELECT role, LEFT(content, 200) AS preview, created_at AS at
FROM conversation_messages
WHERE conversation_id = $1 AND is_summary = FALSE
ORDER BY seq DESC
LIMIT 1
""",
    is_read=True,
)

UPDATE_CONVERSATION_TEMPLATE = SqlQuery(
    name="update_conversation_template",
    sql="""\
UPDATE conversations
SET template_content = $2, updated_at = now()
WHERE id = $1 AND template_content IS NULL
""",
    is_read=False,
)

FETCH_CONVERSATION_TEMPLATE = SqlQuery(
    name="fetch_conversation_template",
    sql="""\
SELECT template_content
FROM conversations
WHERE id = $1 AND user_id = $2
""",
    is_read=True,
)


# ── Investigation queries (generic) ──────────────────────────────────────────

INSERT_INVESTIGATION = SqlQuery(
    name="insert_investigation",
    sql="""\
INSERT INTO investigations (
    id, tenant_id, display_id, type, state, origin,
    assignee_id, version, entity_id, entity_type,
    draft_source, draft_drafted_at, draft_comparison_narrative,
    created_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, 1, $8, $9,
    $10, $11, $12,
    now(), now()
)
""",
    is_read=False,
)

UPDATE_INVESTIGATION_STATE = SqlQuery(
    name="update_investigation_state",
    sql="""\
UPDATE investigations
SET state = $2, updated_at = now()
WHERE id = $1 AND tenant_id = $3
""",
    is_read=False,
)

INSERT_AUDIT_LOG = SqlQuery(
    name="insert_audit_log",
    sql="""\
INSERT INTO investigation_audit_log (
    investigation_id, tenant_id, event_type, actor, text
) VALUES ($1, $2, $3, $4, $5)
""",
    is_read=False,
)


# ── Registry ──────────────────────────────────────────────────────────────────

QUERY_REGISTRY: dict[str, SqlQuery] = {
    q.name: q
    for q in [
        # Conversation
        INSERT_CONVERSATION,
        FETCH_CONVERSATION,
        LIST_CONVERSATIONS,
        COUNT_CONVERSATIONS,
        COUNT_ACTIVE_CONVERSATIONS,
        INSERT_CONVERSATION_MESSAGE,
        FETCH_CONVERSATION_MESSAGES,
        COUNT_CONVERSATION_MESSAGES,
        FETCH_CONVERSATION_RECENT_MESSAGES,
        UPDATE_CONVERSATION_ATOMIC,
        NEXT_MESSAGE_SEQ,
        UPDATE_CONVERSATION_STATUS,
        UPDATE_CONVERSATION_TITLE,
        FETCH_LAST_MESSAGE_PREVIEW,
        UPDATE_CONVERSATION_TEMPLATE,
        FETCH_CONVERSATION_TEMPLATE,
        # Investigation
        INSERT_INVESTIGATION,
        UPDATE_INVESTIGATION_STATE,
        INSERT_AUDIT_LOG,
    ]
}


def get_domain_queries() -> dict[str, SqlQuery]:
    """Return domain-specific queries from the configured QueryProvider."""
    from smi_agent.domain.registry import DomainRegistry

    provider = DomainRegistry.query_provider()
    result = dict(QUERY_REGISTRY)
    # Merge domain read and write queries
    for name, query in provider.read_queries.items():
        result[name] = query
    for name, query in provider.write_queries.items():
        result[name] = query
    return result