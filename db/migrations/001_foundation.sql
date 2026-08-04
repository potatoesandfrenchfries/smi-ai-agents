-- ═════════════════════════════════════════════════════════════════════════════
-- 001 · Foundation — extensions, shared helpers, and the enum vocabulary.
--
-- Every enum in this file names a value that appears verbatim in the design
-- docs (Functional PRD §4/§6/§7, Technical Design Reference §3/§12) or in the
-- Python code that will read these tables. Where the code compares a column
-- against a plain ``varchar`` bind parameter, the column stays ``varchar`` with
-- a CHECK constraint instead of an enum — see 007_conversations.sql for the
-- three tables affected and why.
-- ═════════════════════════════════════════════════════════════════════════════

-- gen_random_uuid() is core from PostgreSQL 13; citext keeps email comparison
-- case-insensitive without scattering lower() through every query.
CREATE EXTENSION IF NOT EXISTS citext;

-- ── Shared helpers ───────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION touch_updated_at() IS
    'BEFORE UPDATE trigger: stamps updated_at. Attached to every mutable table.';


-- ── Tenancy and identity ─────────────────────────────────────────────────────

-- PRD §2: "The policy and compliance agent runs in corporate mode only; leisure
-- or unmanaged contexts skip policy gating but retain budget and consent checks."
CREATE TYPE org_mode AS ENUM ('corporate', 'leisure');

-- PRD §2 personas, verbatim.
CREATE TYPE org_role AS ENUM (
    'travel_manager',
    'traveler',
    'approver',
    'integration_developer',
    'platform_operator'
);

-- PRD §3: the three delivery models a request can arrive through. Recorded on
-- trips and plans so FR-INT-4 ("identical Trip object from every surface") is
-- verifiable from data rather than asserted.
CREATE TYPE delivery_surface AS ENUM ('hosted_ui', 'rest_api', 'headless_mcp', 'headless_a2a');

CREATE TYPE consent_state AS ENUM ('granted', 'revoked', 'expired');


-- ── Configuration (PRD §7.2 "Configured entities") ───────────────────────────

-- PRD §4.3 specialists + the budget/planner roles this codebase already ships
-- (agent_definitions/specialist_*.yaml, graph/itinerary_graph.py).
CREATE TYPE specialist_kind AS ENUM (
    'planner',
    'flight',
    'lodging',
    'activity',
    'ground',
    'dining',
    'policy',
    'budget'
);

-- PRD §7.2 "Segment Types: Flight, hotel, rail / car, dining", widened to the
-- units the itinerary graph actually emits.
CREATE TYPE segment_kind AS ENUM (
    'flight',
    'hotel',
    'rail',
    'car',
    'dining',
    'activity',
    'transfer'
);

-- PRD §7.2 "Constraints: Budget cap, time window, policy rule, …". The tail of
-- that list is filled in from graph/state.py TripConstraints.
CREATE TYPE constraint_kind AS ENUM (
    'budget_cap',
    'time_window',
    'policy_rule',
    'cabin_class',
    'carrier_preference',
    'max_stops',
    'refundable_only',
    'traveler_count',
    'sort_preference',
    'loyalty_program',
    'dietary',
    'accessibility'
);

-- PRD §7.2 "Provider Gateways: GDS gateway, hotel gateway, activity gateway, …"
CREATE TYPE gateway_kind AS ENUM (
    'gds',
    'hotel',
    'activity',
    'ground',
    'dining',
    'geo_routing',
    'currency',
    'weather'
);

-- PRD §8 provider table: "Legacy GDS", "Modern API-first", "Modern aggregator",
-- "Metasearch / aggregator", plus the open-data and mock sources in providers/.
CREATE TYPE provider_kind AS ENUM ('gds', 'api_first', 'aggregator', 'metasearch', 'open_data', 'mock');


-- ── Planning lifecycle ───────────────────────────────────────────────────────

CREATE TYPE trip_status AS ENUM (
    'draft',        -- goal captured, constraints not yet complete (FR-INT-3)
    'validated',    -- typed Constraints set complete (FR-INT-2)
    'planning',     -- a plan run is in flight
    'planned',      -- at least one itinerary version exists
    'confirmed',    -- a segment has been handed off (FR-PRS-2)
    'cancelled',
    'expired'
);

CREATE TYPE plan_status AS ENUM (
    'pending',
    'dispatching',       -- subtasks fanned out (FR-ORC-1)
    'merging',           -- reconciling verified candidates (FR-ORC-4)
    'complete',
    'partial',           -- degraded to partial results (FR-ORC-5)
    'deadline_exceeded', -- per-plan latency deadline breached (FR-ORC-5)
    'budget_exceeded',   -- Design Reference §3.2 financial circuit breaker
    'failed'
);

-- PRD §6.2 reply envelope, ``status`` field — these four values verbatim.
CREATE TYPE task_status AS ENUM ('done', 'partial', 'blocked', 'needs_input');

CREATE TYPE itinerary_status AS ENUM (
    'draft',
    'awaiting_review',    -- HITL gate open (FR-GAT-3)
    'awaiting_approval',  -- breached a threshold, routed to approver (FR-PRS-4)
    'approved',
    'confirmed',          -- human confirmed; handoff permitted (FR-PRS-2)
    'rejected',
    'superseded',         -- a later version replaced it (FR-PRS-1)
    'expired'             -- every backing PriceSnapshot aged out
);

-- graph/state.py ItineraryState.policy_status uses compliant/breach/pending.
CREATE TYPE policy_decision AS ENUM ('compliant', 'breach', 'waived', 'not_applicable', 'pending');

CREATE TYPE approval_state AS ENUM ('pending', 'approved', 'rejected', 'expired');

CREATE TYPE handoff_state AS ENUM (
    'pending',    -- segment verified, link not yet minted
    'ready',      -- link minted, awaiting human action
    'opened',     -- traveler followed the link
    'completed',  -- provider reported the booking done
    'failed',
    'expired'
);


-- ── Event backbone, audit, notifications ─────────────────────────────────────

-- FR-EVT-1: "Publish session.state, plan.events, and provenance to an event
-- backbone consumed across the platform."
CREATE TYPE event_topic AS ENUM ('session.state', 'plan.events', 'provenance');

CREATE TYPE outbox_state AS ENUM ('pending', 'published', 'failed');

-- FR-PRS-5: "Emit notifications (push, in-app, email)".
CREATE TYPE notification_channel AS ENUM ('push', 'in_app', 'email');
CREATE TYPE notification_state AS ENUM ('queued', 'sent', 'failed', 'read');

-- Design Reference §12.1 error categories, used to classify tool and LLM
-- outcomes in the audit trail.
CREATE TYPE call_outcome AS ENUM ('success', 'error', 'denied', 'clamped', 'timeout', 'budget_exceeded');

-- Design Reference §7.1 lane table.
CREATE TYPE llm_lane AS ENUM ('reasoning', 'middle', 'triage', 'air_gap');
