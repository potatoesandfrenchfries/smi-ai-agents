-- ═════════════════════════════════════════════════════════════════════════════
-- 006 · Requested entities, part two: Itinerary, Segment, Booking Handoff,
--       approvals, and notifications.
--
-- PRD §7.1: "Itinerary — Generated for a traveler; comprises ordered segments."
-- "Segment — A bookable unit (flight, hotel, rail/car, dining) attached to a
-- provider." "Booking Handoff — Routes a segment between the itinerary and a
-- provider for booking." FR-PRS-1 requires versioning; the model here treats
-- a version as an immutable row rather than an in-place update, which is what
-- makes "the itinerary the reviewer confirmed" a fact that outlives later edits.
-- ═════════════════════════════════════════════════════════════════════════════

CREATE TABLE itineraries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id             uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    plan_id             uuid NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- FR-PRS-1: "Store the final itinerary as a versioned object". Versions are
    -- append-only; superseding an itinerary sets the old row's status to
    -- 'superseded' rather than overwriting it (see itineraries_supersede below).
    version             integer NOT NULL,

    status              itinerary_status NOT NULL DEFAULT 'draft',

    total_amount        numeric(14, 2) NOT NULL,
    currency            char(3) NOT NULL REFERENCES currencies(code),

    -- FR-PRS-3: "Present ranked options with assumptions, price, and provenance
    -- visible to the reviewer." Assumptions are copied onto the itinerary at
    -- compile time so a reviewer sees them without joining back through every
    -- agent_task_results row.
    assumptions         text[] NOT NULL DEFAULT '{}',

    policy_status       policy_decision NOT NULL DEFAULT 'pending',

    -- FR-PRS-4: populated when budget or policy is breached and the plan
    -- routes to an approver instead of straight to confirmation.
    requires_approval   boolean NOT NULL DEFAULT false,

    -- Stage 6.5 reflection/critic findings from graph/state.py
    -- ItineraryState.quality_review, kept for the audit trail even though the
    -- reviewer mainly sees the resulting text.
    quality_review      jsonb,

    confirmed_by_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at        timestamptz,
    rejected_reason      text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT itineraries_version_unique UNIQUE (trip_id, version),
    CONSTRAINT itineraries_version_positive CHECK (version > 0),
    CONSTRAINT itineraries_total_positive CHECK (total_amount > 0),
    CONSTRAINT itineraries_confirmed_coherent
        CHECK ((status = 'confirmed') = (confirmed_by_id IS NOT NULL AND confirmed_at IS NOT NULL)),
    CONSTRAINT itineraries_rejected_explained
        CHECK (status <> 'rejected' OR rejected_reason IS NOT NULL)
);

COMMENT ON TABLE itineraries IS
    'FR-PRS-1 versioned itinerary. Each edit (HITL or reflection-driven) is a new row via itineraries_supersede(), never an in-place update — so a confirmed version is provably the one the reviewer saw.';
COMMENT ON COLUMN itineraries.assumptions IS
    'FR-PRS-3: assumptions the specialists made, copied at compile time so the reviewer sees them alongside price without an extra join.';

CREATE UNIQUE INDEX itineraries_one_current_per_trip
    ON itineraries (trip_id) WHERE status NOT IN ('superseded', 'expired', 'rejected');

CREATE INDEX itineraries_org_status_idx ON itineraries (org_id, status);
CREATE INDEX itineraries_plan_idx ON itineraries (plan_id);

CREATE TRIGGER itineraries_touch BEFORE UPDATE ON itineraries
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Enforces "versioned object" as a sequence rather than a suggestion: the next
-- version for a trip must be exactly one more than the current max, and the
-- previous current version is retired atomically. Application code calls this
-- instead of INSERTing itineraries directly.
CREATE OR REPLACE FUNCTION itineraries_supersede(
    p_trip_id uuid,
    p_new_row itineraries
) RETURNS itineraries
LANGUAGE plpgsql AS $$
DECLARE
    v_next_version integer;
    v_result itineraries;
BEGIN
    UPDATE itineraries
    SET status = 'superseded', updated_at = now()
    WHERE trip_id = p_trip_id AND status NOT IN ('superseded', 'expired', 'rejected');

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM itineraries WHERE trip_id = p_trip_id;

    INSERT INTO itineraries (
        trip_id, plan_id, org_id, version, status, total_amount, currency,
        assumptions, policy_status, requires_approval, quality_review
    ) VALUES (
        p_trip_id, p_new_row.plan_id, p_new_row.org_id, v_next_version,
        p_new_row.status, p_new_row.total_amount, p_new_row.currency,
        p_new_row.assumptions, p_new_row.policy_status, p_new_row.requires_approval,
        p_new_row.quality_review
    ) RETURNING * INTO v_result;

    RETURN v_result;
END;
$$;

COMMENT ON FUNCTION itineraries_supersede(uuid, itineraries) IS
    'The only sanctioned way to create a new itinerary version: retires the current one and inserts version+1 atomically, so "versioned object" (FR-PRS-1) cannot be bypassed by a bare INSERT.';


-- ── Segments (PRD §7.1: "A bookable unit … attached to a provider") ──────────

CREATE TABLE itinerary_segments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    itinerary_id        uuid NOT NULL REFERENCES itineraries(id) ON DELETE CASCADE,
    candidate_id        uuid NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,

    kind                segment_kind NOT NULL,
    provider_id         uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,

    -- Ordering within the itinerary — a hotel stay and three dinners interleave
    -- with flights, and the reviewer needs to see them chronologically.
    sequence            integer NOT NULL,

    starts_at           timestamptz,
    ends_at             timestamptz,

    title               text NOT NULL,
    amount               numeric(14, 2) NOT NULL,
    currency             char(3) NOT NULL REFERENCES currencies(code),

    details              jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- FR-SPC-2 result carried onto the confirmed segment, distinct from the
    -- candidate's own policy_decision so a later policy version change cannot
    -- retroactively alter what a confirmed segment was approved under.
    policy_decision       policy_decision NOT NULL DEFAULT 'not_applicable',

    created_at            timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT itinerary_segments_unique UNIQUE (itinerary_id, sequence),
    CONSTRAINT itinerary_segments_amount_positive CHECK (amount > 0),
    CONSTRAINT itinerary_segments_span_order CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);

COMMENT ON TABLE itinerary_segments IS
    'PRD §7.1 Segment. One bookable unit per row, ordered by sequence for chronological presentation (FR-PRS-3).';

CREATE INDEX itinerary_segments_itinerary_idx ON itinerary_segments (itinerary_id, sequence);
CREATE INDEX itinerary_segments_provider_idx ON itinerary_segments (provider_id);


-- ── Booking handoffs (PRD §1.3 "Handoff", §7.1 "Booking Handoff") ────────────

CREATE TABLE booking_handoffs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    segment_id          uuid NOT NULL REFERENCES itinerary_segments(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    state               handoff_state NOT NULL DEFAULT 'pending',

    -- FR-PRS-2 / PRD Non-Goals: Smartinerary is not the booking system of
    -- record. This link is where the transaction actually completes.
    handoff_url         text,
    provider_booking_ref text,

    -- FR-GAT-3: no irreversible action without this. Distinct from the
    -- itinerary-level confirmed_by_id/confirmed_at because a traveler can
    -- confirm the itinerary and still handle each segment's handoff separately
    -- (e.g. book the flight now, the hotel tomorrow).
    confirmed_by_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at        timestamptz,

    opened_at           timestamptz,
    completed_at        timestamptz,
    failure_reason      text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT booking_handoffs_confirmed_coherent
        CHECK (state = 'pending' OR state = 'expired' OR (confirmed_by_id IS NOT NULL AND confirmed_at IS NOT NULL)),
    CONSTRAINT booking_handoffs_ready_has_link
        CHECK (state NOT IN ('ready', 'opened', 'completed') OR handoff_url IS NOT NULL),
    CONSTRAINT booking_handoffs_failed_explained
        CHECK (state <> 'failed' OR failure_reason IS NOT NULL)
);

COMMENT ON TABLE booking_handoffs IS
    'PRD §1.3/§7.1 Handoff. Records the transition to the external booking system; Smartinerary is not the system of record for the transaction itself (PRD §1.2 Non-Goals).';
COMMENT ON CONSTRAINT booking_handoffs_confirmed_coherent ON booking_handoffs IS
    'FR-GAT-3: "Require a human-in-the-loop gate before any irreversible or booking-adjacent action" — no handoff progresses past pending without a confirming user and timestamp.';

CREATE UNIQUE INDEX booking_handoffs_one_per_segment ON booking_handoffs (segment_id);
CREATE INDEX booking_handoffs_org_state_idx ON booking_handoffs (org_id, state);

CREATE TRIGGER booking_handoffs_touch BEFORE UPDATE ON booking_handoffs
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Approvals (FR-PRS-4, corporate mode) ─────────────────────────────────────

CREATE TABLE itinerary_approvals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    itinerary_id        uuid NOT NULL REFERENCES itineraries(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Which rule(s) triggered the routing, so the approver sees why they were
    -- asked rather than just that they were.
    triggered_by_rule_id uuid REFERENCES policy_rules(id) ON DELETE SET NULL,
    reason              text NOT NULL,

    -- PRD §12 open question: "Depth of approval workflows (single vs.
    -- multi-step approval chains)." step_number + is_final_step supports both
    -- without a schema change once the answer lands; a single-step chain is
    -- just one row with is_final_step = true.
    step_number         integer NOT NULL DEFAULT 1,
    is_final_step       boolean NOT NULL DEFAULT true,

    approver_id         uuid REFERENCES users(id) ON DELETE SET NULL,
    state               approval_state NOT NULL DEFAULT 'pending',
    decided_at          timestamptz,
    decision_note       text,

    requested_at        timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz,

    CONSTRAINT itinerary_approvals_step_unique UNIQUE (itinerary_id, step_number),
    CONSTRAINT itinerary_approvals_step_positive CHECK (step_number > 0),
    CONSTRAINT itinerary_approvals_decided_coherent
        CHECK (state = 'pending' OR (approver_id IS NOT NULL AND decided_at IS NOT NULL))
);

COMMENT ON TABLE itinerary_approvals IS
    'FR-PRS-4 approval routing. step_number/is_final_step leave room for a multi-step chain (PRD §12 open question) without a future migration.';

CREATE INDEX itinerary_approvals_pending_idx ON itinerary_approvals (org_id, approver_id)
    WHERE state = 'pending';


-- ── Notifications (FR-PRS-5) ──────────────────────────────────────────────────

CREATE TABLE notifications (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- FR-PRS-5 verbatim: "plan ready, approval needed, and handoff complete."
    event_type          text NOT NULL,
    channel             notification_channel NOT NULL,
    state               notification_state NOT NULL DEFAULT 'queued',

    itinerary_id        uuid REFERENCES itineraries(id) ON DELETE CASCADE,
    trip_id             uuid REFERENCES trips(id) ON DELETE CASCADE,

    title               text NOT NULL,
    body                text NOT NULL,

    sent_at             timestamptz,
    read_at             timestamptz,
    failure_reason      text,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT notifications_event_type
        CHECK (event_type IN ('plan_ready', 'approval_needed', 'handoff_complete', 'ceiling_warning', 'ceiling_hit')),
    CONSTRAINT notifications_failed_explained
        CHECK (state <> 'failed' OR failure_reason IS NOT NULL)
);

COMMENT ON TABLE notifications IS
    'FR-PRS-5. One row per (user, channel) delivery, so the same event fanned out to push + email + in-app has independent delivery state.';

CREATE INDEX notifications_user_unread_idx ON notifications (user_id, created_at DESC)
    WHERE state <> 'read';
