-- ═════════════════════════════════════════════════════════════════════════════
-- 007 · Conversation system and workflow investigations.
--
-- Unlike every other migration in this set, the two tables here are NOT new
-- design — they are the durable store the running Python code already talks to
-- via src/smi_agent/postgres_client/queries.py and api/conversation_service.py.
-- Columns, names, and nullability below are taken directly from those call
-- sites (parameter lists in INSERT/UPDATE, .get() calls on result rows) so the
-- existing code runs against this schema unmodified. Where a query file
-- comment says a value is domain-extensible (context_type), this migration
-- deliberately leaves it as an unconstrained column rather than a rigid enum.
--
-- Conversation system → Design Reference §6 (dual-write persistence, usage
-- ceiling, context injection). Investigations → Design Reference §5 (workflow
-- pipelines) and the generic seed framework's audit trail (§10.1, FR-GAT-4).
-- ═════════════════════════════════════════════════════════════════════════════

CREATE TABLE conversations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Opaque identity strings from the auth header (X-Auth-Tenant-Id /
    -- X-Auth-User-Id), not necessarily this schema's organizations.id / users.id
    -- — the conversation surface accepts any authenticated caller shape,
    -- including a headless customer whose IdP subject never resolves into our
    -- own users table. Deliberately text, per Design Reference §3.3 ("tenant
    -- identifier in the auth header... flows through every layer").
    tenant_id               text NOT NULL,
    user_id                 text NOT NULL,

    display_id              text NOT NULL,
    agent_name              text NOT NULL,
    title                   text,

    -- api/models.py _CONV_STATUS: ACTIVE | CEILING_HIT | CLOSED | ARCHIVED.
    status                  text NOT NULL DEFAULT 'ACTIVE',

    -- api/models.py _CONTEXT_TYPES is a base set the domain extends at
    -- runtime (DomainRegistry.entity_resolver().valid_page_types()) — left
    -- unconstrained here rather than a CHECK/enum that would need a migration
    -- every time a domain adds a page type.
    context_type            text,
    context_entity_id       text,
    context_label           varchar(200),

    message_count           integer NOT NULL DEFAULT 0,
    token_count             bigint NOT NULL DEFAULT 0,

    -- Design Reference §6.2 usage ceiling: 80% warning, 100% lock.
    ceiling_max_messages    integer NOT NULL DEFAULT 100,
    ceiling_max_tokens      bigint NOT NULL DEFAULT 200000,
    ceiling_status          text NOT NULL DEFAULT 'ok',
    ceiling_hit_at          timestamptz,
    ceiling_hit_reason      text,

    compaction_count        integer NOT NULL DEFAULT 0,
    compact_summary         text,
    compact_summary_at      timestamptz,
    compact_summary_tokens  integer,

    -- Page-context snapshot rendered once at conversation start (see
    -- api/template_init_service.py) and referenced on later turns without
    -- re-fetching the underlying entity — the "context injection" of Design
    -- Reference §6.3.
    template_content        jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,

    CONSTRAINT conversations_status
        CHECK (status IN ('ACTIVE', 'CEILING_HIT', 'CLOSED', 'ARCHIVED')),
    CONSTRAINT conversations_ceiling_status
        CHECK (ceiling_status IN ('ok', 'warning', 'critical', 'exceeded')),
    CONSTRAINT conversations_counts_non_negative
        CHECK (message_count >= 0 AND token_count >= 0 AND compaction_count >= 0)
);

COMMENT ON TABLE conversations IS
    'Durable half of the dual-write in Design Reference §6.1. Redis holds the fast path (session metadata, recent messages); this table survives a client disconnect and Redis TTL expiry.';
COMMENT ON COLUMN conversations.template_content IS
    'One-time page-context snapshot (Design Reference §6.3 context injection) — set once via UPDATE_CONVERSATION_TEMPLATE (WHERE template_content IS NULL), read on every later turn.';

CREATE INDEX conversations_tenant_user_idx
    ON conversations (tenant_id, user_id, updated_at DESC);
CREATE INDEX conversations_context_idx
    ON conversations (tenant_id, context_type, context_entity_id, context_label);
CREATE INDEX conversations_active_count_idx
    ON conversations (user_id, tenant_id) WHERE status = 'ACTIVE';


CREATE TABLE conversation_messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    -- Monotonic per-conversation sequence, assigned by next_message_seq
    -- (MAX(seq)+1) rather than a global sequence — the ordering only ever
    -- needs to be correct within one conversation.
    seq             integer NOT NULL,
    role            text NOT NULL,
    content         text NOT NULL,
    tokens          integer,

    -- True for the single synthetic message a compaction pass writes in place
    -- of the turns it summarized (Design Reference conversation compaction).
    -- FETCH_CONVERSATION_MESSAGES filters these out; FETCH_CONVERSATION_RECENT
    -- includes them, since the recent-window read needs the compaction anchor.
    is_summary      boolean NOT NULL DEFAULT false,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT conversation_messages_seq_unique UNIQUE (conversation_id, seq),
    CONSTRAINT conversation_messages_role CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    CONSTRAINT conversation_messages_seq_positive CHECK (seq > 0)
);

COMMENT ON TABLE conversation_messages IS
    'Full transcript, one row per turn. seq is per-conversation so FETCH_CONVERSATION_RECENT_MESSAGES can page from the tail without a global ordering column.';

CREATE INDEX conversation_messages_conversation_idx
    ON conversation_messages (conversation_id, seq);


-- ── Investigations: workflow pipeline runs (Design Reference §5) ────────────

CREATE TABLE investigations (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   text NOT NULL,

    display_id                  text NOT NULL,
    type                        text NOT NULL,
    state                       text NOT NULL DEFAULT 'PENDING',
    origin                      text NOT NULL DEFAULT 'MANUAL',

    assignee_id                 text,
    version                     integer NOT NULL DEFAULT 1,

    entity_id                   text NOT NULL,
    entity_type                 text NOT NULL DEFAULT 'entity',

    -- schemas/plan.py PlanEnvelope, persisted once the workflow completes.
    draft_source                text,
    draft_drafted_at            timestamptz,
    draft_comparison_narrative  text,

    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT investigations_version_positive CHECK (version > 0)
);

COMMENT ON TABLE investigations IS
    'One row per workflow pipeline run (Design Reference §5.1 state machine). In the travel domain, itinerary generation is itself a Temporal/LangGraph pipeline — see plans/itineraries in 004/006 for that domain-specific model; this table remains for the generic investigate-an-entity workload the framework also ships.';

CREATE UNIQUE INDEX investigations_tenant_display_idx ON investigations (tenant_id, display_id);
CREATE INDEX investigations_tenant_entity_idx ON investigations (tenant_id, entity_id);
CREATE INDEX investigations_tenant_state_idx ON investigations (tenant_id, state);

CREATE TRIGGER investigations_touch BEFORE UPDATE ON investigations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE investigation_audit_log (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    investigation_id    uuid NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    tenant_id            text NOT NULL,

    -- FR-GAT-4 shape: which event, who/what caused it, and a human-readable
    -- account — matches the 5-column INSERT_AUDIT_LOG in queries.py exactly.
    event_type          text NOT NULL,
    actor               text NOT NULL,
    text                text NOT NULL,

    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE investigation_audit_log IS
    'Per-investigation event trail (state transitions, plan drafted, etc). For the cross-cutting tool-call/LLM-call audit trail (Design Reference §10.1, FR-GAT-4), see tool_call_log in 008_event_backbone_audit.sql — a different log for a different question ("what happened to this investigation" vs "what did every tool call across the platform do").';

CREATE INDEX investigation_audit_log_investigation_idx
    ON investigation_audit_log (investigation_id, created_at);
