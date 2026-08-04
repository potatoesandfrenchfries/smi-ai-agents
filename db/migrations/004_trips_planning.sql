-- ═════════════════════════════════════════════════════════════════════════════
-- 004 · Requested entities, part one: the Trip, the plan run, the PlanGraph,
--       and the A2A task envelopes.
--
-- The distinction that drives this file: a *trip* is what the traveler wants,
-- and a *plan* is one attempt at satisfying it. Re-planning after a HITL edit
-- creates a new plan against the same trip, which is what makes the edit history
-- reconstructable (FR-ORC-6) and keeps A2A tasks idempotent (PRD §6, §10.4).
-- ═════════════════════════════════════════════════════════════════════════════

-- ── Trips (FR-INT-2 "a Trip object with a typed Constraints set") ─────────────

CREATE TABLE trips (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Who travels, and who asked. A travel manager planning on behalf of an
    -- employee (PRD §2) makes these two different people, and approval routing
    -- and consent checks both need to know which is which.
    traveler_id         uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_by_id     uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    display_id          text NOT NULL,

    -- FR-INT-1: the natural-language goal, kept verbatim. Re-parsing the
    -- original text is the only way to answer "why did it think that".
    raw_goal            text NOT NULL,

    status              trip_status NOT NULL DEFAULT 'draft',

    -- FR-INT-4: the same payload from every surface must produce an identical
    -- Trip. Recording the surface is how that gets tested rather than trusted.
    origin_surface      delivery_surface NOT NULL,

    -- Denormalized from trip_constraints because every specialist reads them on
    -- every dispatch, and a join per subtask is a real cost at fan-out width.
    -- trip_constraints remains authoritative for provenance and validation.
    purpose             text,
    origin_city         text,
    origin_iata         char(3),
    destination_city    text,
    destination_iata    char(3),
    depart_date         date,
    return_date         date,
    traveler_count      integer NOT NULL DEFAULT 1,

    budget_amount       numeric(14, 2),
    budget_currency     char(3) REFERENCES currencies(code),
    sort_preference     text NOT NULL DEFAULT 'cost',

    -- FR-INT-2/3: which required constraints are still missing. Non-empty means
    -- the intake must ask rather than guess, and planning must not start.
    missing_fields      text[] NOT NULL DEFAULT '{}',

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT trips_display_id_unique UNIQUE (org_id, display_id),
    CONSTRAINT trips_purpose CHECK (purpose IS NULL OR purpose IN ('business', 'leisure')),
    CONSTRAINT trips_sort_preference
        CHECK (sort_preference IN ('cost', 'comfort', 'time', 'rating', 'match')),
    CONSTRAINT trips_traveler_count_positive CHECK (traveler_count > 0),
    CONSTRAINT trips_date_order CHECK (return_date IS NULL OR depart_date IS NULL OR return_date >= depart_date),
    CONSTRAINT trips_budget_pair CHECK ((budget_amount IS NULL) = (budget_currency IS NULL)),
    CONSTRAINT trips_budget_positive CHECK (budget_amount IS NULL OR budget_amount > 0),
    CONSTRAINT trips_origin_iata CHECK (origin_iata IS NULL OR origin_iata ~ '^[A-Z]{3}$'),
    CONSTRAINT trips_destination_iata CHECK (destination_iata IS NULL OR destination_iata ~ '^[A-Z]{3}$'),

    -- FR-INT-2: a trip may only leave draft once nothing required is missing.
    CONSTRAINT trips_validated_is_complete
        CHECK (status = 'draft' OR cardinality(missing_fields) = 0)
);

COMMENT ON TABLE trips IS
    'The Trip object of FR-INT-2: a natural-language goal plus a validated constraint set. One trip, many plan attempts.';
COMMENT ON COLUMN trips.raw_goal IS
    'The traveler''s original wording, never overwritten. Needed to explain any inference the intake parser made.';
COMMENT ON COLUMN trips.missing_fields IS
    'Required constraint codes still unanswered. Non-empty blocks planning and drives the FR-INT-3 prompt.';
COMMENT ON CONSTRAINT trips_validated_is_complete ON trips IS
    'FR-INT-2: "validate completeness before planning starts" — enforced in the schema, not only in the parser.';

CREATE INDEX trips_org_traveler_idx ON trips (org_id, traveler_id, created_at DESC);
CREATE INDEX trips_org_status_idx ON trips (org_id, status) WHERE status IN ('planning', 'planned');

CREATE TRIGGER trips_touch BEFORE UPDATE ON trips
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Trip constraints (the typed Constraints set, normalized) ──────────────────

CREATE TABLE trip_constraints (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id         uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    definition_id   uuid NOT NULL REFERENCES constraint_definitions(id) ON DELETE RESTRICT,

    kind            constraint_kind NOT NULL,
    value           jsonb NOT NULL,

    -- FR-SPC-3 returns assumptions[]; this is the input-side mirror. A
    -- constraint the parser inferred rather than read must be visible to the
    -- reviewer, because an inferred hard rule is the likeliest thing to be wrong.
    is_inferred     boolean NOT NULL DEFAULT false,
    inferred_from   text,

    -- Where it came from: the traveler, the org policy, or the traveler's
    -- standing preferences. Determines who can relax it.
    source          text NOT NULL DEFAULT 'traveler',
    policy_rule_id  uuid REFERENCES policy_rules(id) ON DELETE SET NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT trip_constraints_unique UNIQUE (trip_id, definition_id),
    CONSTRAINT trip_constraints_source CHECK (source IN ('traveler', 'policy', 'preference', 'operator')),
    CONSTRAINT trip_constraints_inference_explained
        CHECK (NOT is_inferred OR inferred_from IS NOT NULL),
    CONSTRAINT trip_constraints_policy_source
        CHECK ((source = 'policy') = (policy_rule_id IS NOT NULL))
);

COMMENT ON TABLE trip_constraints IS
    'One row per constraint on a trip. Authoritative over the denormalized columns on trips, which exist only for dispatch-path speed.';
COMMENT ON COLUMN trip_constraints.is_inferred IS
    'True when the parser derived this rather than being told. Surfaced to the reviewer beside the itinerary, since a wrong inferred hard rule silently narrows every search.';


-- ── Plans: one planning run against a trip ───────────────────────────────────

CREATE TABLE plans (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id             uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- graph/state.py ItineraryState.plan_id doubles as the idempotency key, and
    -- Temporal uses it as the workflow id. Unique so a retried submission
    -- rejoins the existing run instead of starting a second one.
    plan_key            text NOT NULL UNIQUE,

    attempt             integer NOT NULL DEFAULT 1,
    status              plan_status NOT NULL DEFAULT 'pending',
    origin_surface      delivery_surface NOT NULL,

    -- Why this attempt exists. A HITL edit re-run carries the edit that caused
    -- it, which is what makes the revision history readable.
    trigger             text NOT NULL DEFAULT 'initial',
    parent_plan_id      uuid REFERENCES plans(id) ON DELETE SET NULL,
    edit_request        jsonb,

    -- FR-ORC-5: per-plan latency deadline and per-subtask budget.
    deadline_seconds    integer NOT NULL DEFAULT 120,
    deadline_at         timestamptz,
    budget_allowance_usd numeric(10, 4) NOT NULL,
    budget_spent_usd    numeric(10, 4) NOT NULL DEFAULT 0,
    tokens_spent        bigint NOT NULL DEFAULT 0,

    -- FX rate set used to normalize this plan's prices (FR-INT-5). Pinned so
    -- the plan's arithmetic stays reproducible.
    fx_as_of            timestamptz,

    -- Temporal correlation, so a plan row can be traced to its workflow history.
    workflow_id         text,
    workflow_run_id     text,

    started_at          timestamptz,
    finished_at         timestamptz,
    error_summary       text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT plans_trip_attempt_unique UNIQUE (trip_id, attempt),
    CONSTRAINT plans_attempt_positive CHECK (attempt > 0),
    CONSTRAINT plans_trigger CHECK (trigger IN ('initial', 'hitl_edit', 'reflection', 'snapshot_expired', 'retry', 'approval_rework')),
    CONSTRAINT plans_budget_positive CHECK (budget_allowance_usd > 0),
    CONSTRAINT plans_spend_non_negative CHECK (budget_spent_usd >= 0 AND tokens_spent >= 0),
    CONSTRAINT plans_deadline_positive CHECK (deadline_seconds > 0),
    CONSTRAINT plans_edit_has_parent
        CHECK (trigger <> 'hitl_edit' OR (parent_plan_id IS NOT NULL AND edit_request IS NOT NULL)),
    CONSTRAINT plans_terminal_has_finish
        CHECK (status NOT IN ('complete', 'partial', 'failed', 'deadline_exceeded', 'budget_exceeded')
               OR finished_at IS NOT NULL),
    CONSTRAINT plans_failure_explained
        CHECK (status <> 'failed' OR error_summary IS NOT NULL)
);

COMMENT ON TABLE plans IS
    'One attempt at satisfying a trip. A HITL edit produces a new attempt rather than mutating the last, so the revision chain stays intact.';
COMMENT ON COLUMN plans.plan_key IS
    'Idempotency key, also the Temporal workflow id. A resubmitted request rejoins this run instead of double-spending (PRD §10.4).';
COMMENT ON COLUMN plans.edit_request IS
    'The HITL change that caused this attempt, e.g. {"section":"flight","candidate_id":"...","note":"earlier departure"}.';
COMMENT ON CONSTRAINT plans_failure_explained ON plans IS
    'Design Reference §3.5 Fail Visible: a failed plan that cannot say why is not observable.';

CREATE INDEX plans_trip_idx ON plans (trip_id, attempt DESC);
CREATE INDEX plans_org_status_idx ON plans (org_id, status, created_at DESC);
CREATE INDEX plans_workflow_idx ON plans (workflow_id) WHERE workflow_id IS NOT NULL;
CREATE INDEX plans_in_flight_idx ON plans (deadline_at)
    WHERE status IN ('pending', 'dispatching', 'merging');

CREATE TRIGGER plans_touch BEFORE UPDATE ON plans
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── PlanGraph (FR-ORC-6) ─────────────────────────────────────────────────────
--
-- "Maintain a PlanGraph capturing the decomposition, dispatch, and merge so any
-- plan can be reconstructed." Stored as nodes and edges rather than one JSON
-- document, because "reconstruct" means queryable: which specialists ran, in
-- what order, which branch was skipped, where the merge pulled from.

CREATE TABLE plan_graph_nodes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id         uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,

    -- Matches the LangGraph node name in graph/itinerary_graph.py, e.g.
    -- parse_intent, search_business_specialists, merge_results, policy_check.
    node_key        text NOT NULL,
    node_type       text NOT NULL,
    sequence        integer NOT NULL,

    status          text NOT NULL DEFAULT 'pending',

    -- Per-node budget, per Design Reference §5.1: "Budget tracking is per-node
    -- so expensive steps can have tighter limits."
    budget_allowance_usd numeric(10, 4),
    cost_usd        numeric(10, 4) NOT NULL DEFAULT 0,
    tokens_used     integer NOT NULL DEFAULT 0,

    started_at      timestamptz,
    finished_at     timestamptz,
    duration_ms     integer,
    error           text,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT plan_graph_nodes_unique UNIQUE (plan_id, node_key, sequence),
    CONSTRAINT plan_graph_nodes_type
        CHECK (node_type IN ('intake', 'decompose', 'dispatch', 'specialist', 'merge', 'policy', 'budget', 'compile', 'reflect', 'gate')),
    CONSTRAINT plan_graph_nodes_status
        CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')),
    CONSTRAINT plan_graph_nodes_failure_explained
        CHECK (status <> 'failed' OR error IS NOT NULL)
);

COMMENT ON TABLE plan_graph_nodes IS
    'FR-ORC-6 PlanGraph vertices. node_key matches the LangGraph node name so a stored graph and a running graph are comparable.';

CREATE INDEX plan_graph_nodes_plan_idx ON plan_graph_nodes (plan_id, sequence);


CREATE TABLE plan_graph_edges (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id         uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    from_node_id    uuid NOT NULL REFERENCES plan_graph_nodes(id) ON DELETE CASCADE,
    to_node_id      uuid NOT NULL REFERENCES plan_graph_nodes(id) ON DELETE CASCADE,

    -- Conditional edges are the interesting ones: recording which branch was
    -- taken, and the condition that chose it, is most of what reconstruction is
    -- for. The itinerary graph branches on trip type, policy status, and the
    -- reflection verdict.
    edge_type       text NOT NULL DEFAULT 'sequential',
    condition_label text,
    was_traversed   boolean NOT NULL DEFAULT true,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT plan_graph_edges_unique UNIQUE (from_node_id, to_node_id),
    CONSTRAINT plan_graph_edges_no_self_loop CHECK (from_node_id <> to_node_id),
    CONSTRAINT plan_graph_edges_type CHECK (edge_type IN ('sequential', 'conditional', 'fanout', 'fanin')),
    CONSTRAINT plan_graph_edges_condition_labelled
        CHECK (edge_type <> 'conditional' OR condition_label IS NOT NULL)
);

COMMENT ON TABLE plan_graph_edges IS
    'FR-ORC-6 PlanGraph edges, including branches not taken (was_traversed = false) so a reconstruction shows the road not travelled.';

CREATE INDEX plan_graph_edges_plan_idx ON plan_graph_edges (plan_id);


-- ── A2A request envelope (PRD §6.1) ──────────────────────────────────────────

CREATE TABLE agent_tasks (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id             uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    node_id             uuid REFERENCES plan_graph_nodes(id) ON DELETE SET NULL,

    -- PRD §6.1 fields, one column each.
    task_key            text NOT NULL,                  -- task_id: idempotent unit of work
    goal                text NOT NULL,                  -- what to satisfy, in scope terms
    constraints         jsonb NOT NULL DEFAULT '{}'::jsonb,
    context_ref         text NOT NULL,                  -- pointer to shared state, never a dump
    deadline_at         timestamptz NOT NULL,
    budget_remaining_usd numeric(10, 4) NOT NULL,

    role_id             uuid NOT NULL REFERENCES specialist_roles(id) ON DELETE RESTRICT,
    segment_kind        segment_kind,

    dispatched_at       timestamptz NOT NULL DEFAULT now(),

    -- PRD §6: "One task equals one specialist call; tasks are idempotent."
    -- Uniqueness on the key is what makes a retry safe (PRD §10.4).
    CONSTRAINT agent_tasks_key_unique UNIQUE (task_key),
    CONSTRAINT agent_tasks_budget_non_negative CHECK (budget_remaining_usd >= 0),

    -- FR-ORC-3: "Pass a pointer to shared state (context_ref), never a full
    -- context dump." A short pointer is the observable form of that rule; a
    -- context dump would not fit in 512 characters.
    CONSTRAINT agent_tasks_context_ref_is_pointer CHECK (length(context_ref) <= 512)
);

COMMENT ON TABLE agent_tasks IS
    'PRD §6.1 request envelope, one row per orchestrator-to-specialist dispatch. task_key uniqueness makes retries idempotent (PRD §10.4).';
COMMENT ON COLUMN agent_tasks.context_ref IS
    'Pointer into the shared backplane (FR-ORC-3, FR-EVT-3), e.g. plan:<plan_key>:state. Never the context itself.';
COMMENT ON CONSTRAINT agent_tasks_context_ref_is_pointer ON agent_tasks IS
    'FR-ORC-3 made mechanical: a length ceiling no context dump can pass.';

CREATE INDEX agent_tasks_plan_idx ON agent_tasks (plan_id, dispatched_at);
CREATE INDEX agent_tasks_org_role_idx ON agent_tasks (org_id, role_id, dispatched_at DESC);


-- ── A2A reply envelope (PRD §6.2) ────────────────────────────────────────────

CREATE TABLE agent_task_results (
    task_id         uuid PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
    plan_id         uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,

    -- PRD §6.2 fields.
    status          task_status NOT NULL,
    assumptions     text[] NOT NULL DEFAULT '{}',
    cost_usd        numeric(10, 4) NOT NULL DEFAULT 0,
    tool_calls      integer NOT NULL DEFAULT 0,
    tokens_used     integer NOT NULL DEFAULT 0,
    confidence      numeric(3, 2) NOT NULL DEFAULT 1.00,

    -- candidates[] lives in its own table; provenance hangs off each candidate
    -- so FR-SPC-4 can be enforced per option rather than per reply.
    candidate_count integer NOT NULL DEFAULT 0,

    -- Populated when status is 'needs_input' (PRD §6.2) so the orchestrator can
    -- surface a question instead of a dead end.
    needs_input_fields text[] NOT NULL DEFAULT '{}',
    blocked_reason  text,

    duration_ms     integer,
    received_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT agent_task_results_confidence CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT agent_task_results_non_negative
        CHECK (cost_usd >= 0 AND tool_calls >= 0 AND tokens_used >= 0 AND candidate_count >= 0),
    CONSTRAINT agent_task_results_blocked_explained
        CHECK (status <> 'blocked' OR blocked_reason IS NOT NULL),
    CONSTRAINT agent_task_results_needs_input_named
        CHECK (status <> 'needs_input' OR cardinality(needs_input_fields) > 0),
    CONSTRAINT agent_task_results_done_has_candidates
        CHECK (status <> 'done' OR candidate_count > 0)
);

COMMENT ON TABLE agent_task_results IS
    'PRD §6.2 reply envelope. One row per task — the primary key is the task, which is what "one task equals one specialist call" means in storage.';
COMMENT ON CONSTRAINT agent_task_results_done_has_candidates ON agent_task_results IS
    'A specialist reporting done with nothing to show is a partial result mislabelled; catching it here keeps FR-ORC-5 degradation honest.';

CREATE INDEX agent_task_results_plan_status_idx ON agent_task_results (plan_id, status);
