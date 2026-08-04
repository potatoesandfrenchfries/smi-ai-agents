-- ═════════════════════════════════════════════════════════════════════════════
-- 008 · Event backbone, tool-call audit trail, and budget ledger.
--
-- FR-EVT-1: "Publish session.state, plan.events, and provenance to an event
-- backbone consumed across the platform." This is implemented as a
-- transactional outbox — the same transaction that changes plan/itinerary
-- state inserts the event row, and a separate publisher ships it to Redis
-- pub/sub (the mechanism the code already uses — see conversation/sse.py,
-- streaming/step_emitter.py) or an external bus. That split is what makes
-- FR-EVT-2 ("a plan survives client disconnect and can be resumed") true: the
-- event exists durably even if no one was subscribed when it was published.
--
-- Design Reference §3.1/§11: "every tool call is logged... to the tool-call
-- log", "Fail Visible". tool_call_log is that log, general enough to cover
-- both the conversational ToolRegistry and the itinerary graph's specialist
-- calls.
-- ═════════════════════════════════════════════════════════════════════════════

CREATE TABLE event_outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id          uuid REFERENCES organizations(id) ON DELETE CASCADE,

    topic           event_topic NOT NULL,
    event_type      text NOT NULL,

    -- What the event is about, kept generic since session.state, plan.events,
    -- and provenance each key off a different entity.
    subject_type    text NOT NULL,
    subject_id      text NOT NULL,

    payload         jsonb NOT NULL,

    state           outbox_state NOT NULL DEFAULT 'pending',
    publish_attempts integer NOT NULL DEFAULT 0,
    published_at    timestamptz,
    last_error      text,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT event_outbox_attempts_non_negative CHECK (publish_attempts >= 0),
    CONSTRAINT event_outbox_failed_explained CHECK (state <> 'failed' OR last_error IS NOT NULL)
);

COMMENT ON TABLE event_outbox IS
    'FR-EVT-1 event backbone, transactional-outbox style: written in the same transaction as the state change it describes, then relayed by a publisher process. Backs FR-EVT-2 resumability — the event outlives any particular subscriber.';
COMMENT ON COLUMN event_outbox.subject_type IS
    'e.g. plan, itinerary, trip, conversation — whatever entity the payload describes.';

-- The publisher's core query: oldest pending events first, one topic at a
-- time so a slow consumer on one topic cannot starve the others.
CREATE INDEX event_outbox_pending_idx ON event_outbox (topic, id) WHERE state = 'pending';
CREATE INDEX event_outbox_subject_idx ON event_outbox (subject_type, subject_id, created_at DESC);


-- ── Tool-call audit trail (Design Reference §3.1, §10.1, FR-GAT-4) ──────────

CREATE TABLE tool_call_log (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              uuid REFERENCES organizations(id) ON DELETE CASCADE,

    -- Correlation to whichever workload made the call — a conversation turn or
    -- a plan's specialist dispatch. Both are nullable because a call may occur
    -- outside either (e.g. a platform operator's ad hoc diagnostic query).
    conversation_id     uuid REFERENCES conversations(id) ON DELETE SET NULL,
    plan_id             uuid REFERENCES plans(id) ON DELETE SET NULL,
    agent_task_id        uuid REFERENCES agent_tasks(id) ON DELETE SET NULL,

    agent_name           text NOT NULL,
    tool_name            text NOT NULL,

    -- FR-GAT-4: "agent, arguments, result size, cost, and outcome." Arguments
    -- are logged post-clamping/post-default-injection (Design Reference §8.2,
    -- §8.3) — i.e. what actually ran, not the LLM's raw ask, since the raw ask
    -- may contain "<UNKNOWN>" placeholders the registry already replaced.
    arguments            jsonb NOT NULL DEFAULT '{}'::jsonb,
    was_clamped          boolean NOT NULL DEFAULT false,

    outcome              call_outcome NOT NULL,
    result_size_bytes    integer,
    error_message        text,

    duration_ms          integer,
    tokens_used          integer,
    cost_usd             numeric(10, 6),

    -- FR-GAT-1/2: the allowlist decision this call was checked against, kept
    -- so a denial is explainable after the fact without reconstructing the
    -- agent's YAML as it stood at call time.
    was_allowlisted      boolean NOT NULL DEFAULT true,

    created_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT tool_call_log_error_explained CHECK (outcome <> 'error' OR error_message IS NOT NULL),
    CONSTRAINT tool_call_log_denied_not_allowlisted CHECK (outcome <> 'denied' OR NOT was_allowlisted)
);

COMMENT ON TABLE tool_call_log IS
    'FR-GAT-4 audit trail, one row per tool invocation from any agent (conversational ToolRegistry or itinerary specialist). Design Reference §3.5 Fail Visible: every failure mode (error, denied, clamped, timeout, budget_exceeded) is a distinct, queryable outcome rather than a caught-and-discarded exception.';

CREATE INDEX tool_call_log_org_created_idx ON tool_call_log (org_id, created_at DESC);
CREATE INDEX tool_call_log_conversation_idx ON tool_call_log (conversation_id, created_at) WHERE conversation_id IS NOT NULL;
CREATE INDEX tool_call_log_plan_idx ON tool_call_log (plan_id, created_at) WHERE plan_id IS NOT NULL;
CREATE INDEX tool_call_log_denied_idx ON tool_call_log (org_id, created_at DESC) WHERE outcome = 'denied';


-- ── Budget ledger (Design Reference §3.2 financial circuit breaker) ─────────
--
-- One append-only row per spend event, rather than only a running total on
-- plans/organizations. A running total answers "how much has been spent"; a
-- ledger additionally answers "spent on what, and can I reconstruct the
-- total from scratch" — the second question is what an audit actually asks.

CREATE TABLE budget_ledger_entries (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    plan_id         uuid REFERENCES plans(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES conversations(id) ON DELETE CASCADE,
    agent_task_id   uuid REFERENCES agent_tasks(id) ON DELETE SET NULL,

    llm_lane        llm_lane,
    tokens_input    integer NOT NULL DEFAULT 0,
    tokens_output   integer NOT NULL DEFAULT 0,
    cost_usd        numeric(10, 6) NOT NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT budget_ledger_entries_scope
        CHECK (plan_id IS NOT NULL OR conversation_id IS NOT NULL),
    CONSTRAINT budget_ledger_entries_non_negative
        CHECK (tokens_input >= 0 AND tokens_output >= 0 AND cost_usd >= 0)
);

COMMENT ON TABLE budget_ledger_entries IS
    'Append-only spend log backing Design Reference §3.2 budget enforcement. plans.budget_spent_usd and organizations monthly counters are maintained totals for fast reads; this table is what an audit sums from scratch to verify them.';

CREATE INDEX budget_ledger_entries_org_idx ON budget_ledger_entries (org_id, created_at DESC);
CREATE INDEX budget_ledger_entries_plan_idx ON budget_ledger_entries (plan_id) WHERE plan_id IS NOT NULL;

-- Monthly spend per tenant, the number organizations.monthly_cost_allowance_usd
-- is checked against. A materialized view refreshed on a schedule is enough
-- here — Design Reference performance notes put LLM latency, not a rollup
-- query, as the dominant cost in every request.
CREATE MATERIALIZED VIEW org_monthly_spend AS
SELECT
    org_id,
    date_trunc('month', created_at) AS month,
    sum(cost_usd)                    AS total_cost_usd,
    sum(tokens_input + tokens_output) AS total_tokens
FROM budget_ledger_entries
GROUP BY org_id, date_trunc('month', created_at);

CREATE UNIQUE INDEX org_monthly_spend_idx ON org_monthly_spend (org_id, month);

COMMENT ON MATERIALIZED VIEW org_monthly_spend IS
    'Refresh via REFRESH MATERIALIZED VIEW CONCURRENTLY org_monthly_spend on a schedule (e.g. hourly). Backs the monthly_token_allowance / monthly_cost_allowance_usd check on organizations.';
