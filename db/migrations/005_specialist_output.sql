-- ═════════════════════════════════════════════════════════════════════════════
-- 005 · Specialist output: candidates, PriceSnapshots, and provenance.
--
-- FR-SPC-4 is the load-bearing rule here: "Trace every candidate to one or more
-- PriceSnapshots via provenance; no option is returned without a snapshot."
-- That is written as a hard constraint below (candidates_has_provenance), not
-- left to application discipline, because it is also Acceptance Criterion 2.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── PriceSnapshots (PRD §1.3, FR-EVT-4) ──────────────────────────────────────

CREATE TABLE price_snapshots (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider_id         uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,

    segment_kind        segment_kind NOT NULL,

    -- The provider's own identifier for what was quoted (fare key, rate plan
    -- id, …), kept so a snapshot can be re-fetched or disputed against the
    -- provider's own records.
    provider_ref        text NOT NULL,

    amount              numeric(14, 2) NOT NULL,
    currency            char(3) NOT NULL REFERENCES currencies(code),

    -- TTL-bound, per PRD §1.3 "PriceSnapshot: A time-keyed, TTL-bound record of
    -- provider pricing". captured_at + ttl is what expires_at derives from;
    -- both are kept so a snapshot's remaining life is a plain column read.
    captured_at         timestamptz NOT NULL DEFAULT now(),
    ttl_seconds         integer NOT NULL,
    expires_at          timestamptz NOT NULL,

    -- Raw provider response, kept for dispute resolution and re-verification.
    -- Never shown to the traveler directly — the candidate's own fields are
    -- the presentation layer.
    raw_response        jsonb,

    request_fingerprint text NOT NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT price_snapshots_amount_positive CHECK (amount > 0),
    CONSTRAINT price_snapshots_ttl_positive CHECK (ttl_seconds > 0),
    CONSTRAINT price_snapshots_expiry_coherent CHECK (expires_at = captured_at + make_interval(secs => ttl_seconds))
);

COMMENT ON TABLE price_snapshots IS
    'PRD §1.3 PriceSnapshot. TTL-bound so a stale quote is detectable by a plain expires_at comparison, backing FR-EVT-4 inventory caching.';
COMMENT ON COLUMN price_snapshots.request_fingerprint IS
    'Matches cache/redis_cache.py fingerprint() — the same hash used to key the Redis cache-aside layer, so a DB snapshot and a cache hit trace to the same request.';

CREATE INDEX price_snapshots_org_provider_idx ON price_snapshots (org_id, provider_id, captured_at DESC);
CREATE INDEX price_snapshots_expiry_idx ON price_snapshots (expires_at);
CREATE INDEX price_snapshots_fingerprint_idx ON price_snapshots (request_fingerprint);


-- ── Candidates (PRD §6.2 "candidates[]: Ranked, verified options") ───────────

CREATE TABLE candidates (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             uuid NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    plan_id             uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,

    segment_kind        segment_kind NOT NULL,
    provider_id         uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,

    -- Rank within its specialist's reply, 1 = best by that specialist's own
    -- sort_preference. Ties broken by candidate id for determinism.
    rank                integer NOT NULL,

    -- The candidate's own identity in the provider's system (e.g. a fare offer
    -- id), distinct from provider_ref on the snapshot, which identifies what
    -- was quoted rather than what was offered.
    provider_candidate_ref text NOT NULL,

    title               text NOT NULL,
    summary             text,

    amount              numeric(14, 2) NOT NULL,
    currency            char(3) NOT NULL REFERENCES currencies(code),

    -- Structured, segment-specific fields (flight numbers, cabin, stops;
    -- room type, refundability; cuisine, seating) live here rather than as
    -- one column per segment kind.
    structured_details  jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- FR-SPC-3: specialist self-assessment on this specific option.
    confidence          numeric(3, 2) NOT NULL DEFAULT 1.00,

    -- FR-SPC-2: whether this option cleared the policy specialist. NULL means
    -- not yet evaluated (leisure mode, or evaluation still pending).
    policy_decision      policy_decision NOT NULL DEFAULT 'pending',
    policy_rule_id       uuid REFERENCES policy_rules(id) ON DELETE SET NULL,
    policy_note          text,

    is_selected          boolean NOT NULL DEFAULT false,

    created_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT candidates_rank_positive CHECK (rank > 0),
    CONSTRAINT candidates_amount_positive CHECK (amount > 0),
    CONSTRAINT candidates_confidence CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT candidates_task_rank_unique UNIQUE (task_id, rank),
    CONSTRAINT candidates_policy_breach_explained
        CHECK (policy_decision <> 'breach' OR policy_rule_id IS NOT NULL)
);

COMMENT ON TABLE candidates IS
    'Ranked options returned by a specialist (PRD §6.2). Every row must carry at least one provenance link to a PriceSnapshot — see candidate_provenance and the enforcing trigger below.';
COMMENT ON COLUMN candidates.structured_details IS
    'Segment-specific presentation fields, e.g. {"flight_number":"BA123","stops":0,"cabin":"ECONOMY"} or {"room_type":"double","refundable":true}.';

CREATE INDEX candidates_plan_kind_idx ON candidates (plan_id, segment_kind, rank);
CREATE INDEX candidates_selected_idx ON candidates (plan_id) WHERE is_selected;


-- ── Provenance (FR-SPC-4) ─────────────────────────────────────────────────────

CREATE TABLE candidate_provenance (
    candidate_id        uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    price_snapshot_id   uuid NOT NULL REFERENCES price_snapshots(id) ON DELETE RESTRICT,

    -- A candidate can be backed by more than one snapshot (e.g. a hotel rate
    -- plus a linked breakfast add-on quoted separately); role says which part.
    role                text NOT NULL DEFAULT 'primary',

    created_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (candidate_id, price_snapshot_id)
);

COMMENT ON TABLE candidate_provenance IS
    'FR-SPC-4: every candidate traces to at least one PriceSnapshot. Enforced by candidates_require_provenance below, not left to application discipline.';

CREATE INDEX candidate_provenance_snapshot_idx ON candidate_provenance (price_snapshot_id);

-- FR-SPC-4 / Acceptance Criterion 2, made structural: a candidate with zero
-- provenance rows cannot exist once this trigger is attached. A plain FK or
-- NOT NULL column cannot express "at least one row in another table", so this
-- is the smallest mechanism that can enforce it at the database layer.
CREATE OR REPLACE FUNCTION enforce_candidate_has_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM candidate_provenance WHERE candidate_id = NEW.id
    ) THEN
        RAISE EXCEPTION
            'FR-SPC-4 violation: candidate % has no PriceSnapshot provenance', NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION enforce_candidate_has_provenance() IS
    'FR-SPC-4 / Acceptance Criterion 2: "no option is returned without a snapshot." Runs AFTER INSERT so the candidate and its first provenance row can be written in the same transaction, in either order, and still pass.';

-- Deferred to transaction end: application code inserts the candidate, then
-- its provenance row(s), in one transaction. An immediate check would reject
-- the candidate insert before the provenance row exists.
CREATE CONSTRAINT TRIGGER candidates_require_provenance
    AFTER INSERT ON candidates
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_candidate_has_provenance();
