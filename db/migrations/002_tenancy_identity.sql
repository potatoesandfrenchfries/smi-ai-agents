-- ═════════════════════════════════════════════════════════════════════════════
-- 002 · Tenancy and identity.
--
-- Smartinerary is sold to organizations, never to consumers (PRD §1), so the
-- organization is the root of every ownership chain. Design Reference §3.3 and
-- §14 are emphatic that tenancy is derived from the authenticated connection
-- and never from a request body or model output: that rule lives in the API
-- layer, and this schema's job is to make the rule cheap to enforce — every
-- tenant-scoped table carries org_id and indexes it first.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── Organizations (the tenant) ───────────────────────────────────────────────

CREATE TABLE organizations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                    citext NOT NULL UNIQUE,
    name                    text NOT NULL,
    mode                    org_mode NOT NULL DEFAULT 'corporate',
    is_active               boolean NOT NULL DEFAULT true,

    -- FR-INT-5 / FR-HDL-5: budgets are per-organization and multi-currency.
    base_currency           char(3) NOT NULL DEFAULT 'GBP',

    -- Design Reference §3.2: budgets live in configuration, not code. These are
    -- the tenant-level ceilings a plan or headless session inherits when it
    -- does not set its own.
    default_plan_budget_usd numeric(10, 4) NOT NULL DEFAULT 1.0000,
    default_plan_deadline_s integer NOT NULL DEFAULT 120,
    monthly_token_allowance bigint,
    monthly_cost_allowance_usd numeric(12, 4),

    -- FR-HDL-7: "Let an operator enable, disable, and scope which capabilities
    -- are exposed headlessly per customer."
    headless_enabled        boolean NOT NULL DEFAULT false,
    headless_capabilities   text[] NOT NULL DEFAULT '{}',

    -- PRD §12: PriceSnapshot and tool-call retention is per tenant and region.
    data_region             text NOT NULL DEFAULT 'eu-west',
    snapshot_retention_days integer NOT NULL DEFAULT 30,
    tool_log_retention_days integer NOT NULL DEFAULT 365,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT organizations_currency_iso CHECK (base_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT organizations_positive_budget CHECK (default_plan_budget_usd > 0),
    CONSTRAINT organizations_positive_deadline CHECK (default_plan_deadline_s > 0)
);

COMMENT ON TABLE organizations IS
    'The tenant. Root of every ownership chain; PRD §1 (B2B only, no direct consumer access).';
COMMENT ON COLUMN organizations.mode IS
    'corporate enables the policy specialist and approval routing; leisure skips policy gating but keeps budget and consent checks (PRD §2).';
COMMENT ON COLUMN organizations.headless_capabilities IS
    'Allowlist of MCP/A2A capability names exposed to this tenant (FR-HDL-7). Empty means "all", gated by headless_enabled.';

CREATE TRIGGER organizations_touch BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Users ────────────────────────────────────────────────────────────────────

-- Deliberately thin. Smartinerary authenticates against an external identity
-- provider (PRD §9 step 5), so this table holds the platform's own projection
-- of a principal: the subject claim it trusts, and what the platform lets that
-- subject do. No password column — there are no local credentials by design.
CREATE TABLE users (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- The `sub` claim from the identity provider. Unique per issuer, not
    -- globally, so the pair is what we constrain.
    idp_issuer          text NOT NULL,
    idp_subject         text NOT NULL,

    email               citext,
    display_name        text NOT NULL,
    role                org_role NOT NULL DEFAULT 'traveler',
    is_active           boolean NOT NULL DEFAULT true,
    last_seen_at        timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT users_idp_identity_unique UNIQUE (idp_issuer, idp_subject)
);

COMMENT ON TABLE users IS
    'Platform projection of an IdP principal (PRD §9). Scoped to one organization; a person at two customers is two rows.';
COMMENT ON COLUMN users.role IS
    'PRD §2 persona. Drives approval eligibility (approver) and configuration access (platform_operator).';

CREATE INDEX users_org_role_idx ON users (org_id, role) WHERE is_active;
CREATE UNIQUE INDEX users_org_email_idx ON users (org_id, email) WHERE email IS NOT NULL;

CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Machine credentials for REST and headless surfaces ───────────────────────

-- FR-HDL-3: "Authenticate each headless connection to a customer organization
-- and scope all access to that tenant." The org_id on this row is the single
-- source of tenancy for every request the credential makes — which is what lets
-- the API refuse to read tenant from the body (Design Reference §14).
CREATE TABLE api_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    label           text NOT NULL,
    surface         delivery_surface NOT NULL,

    -- Only the hash is stored. The plaintext key is shown once at creation and
    -- never persisted (Design Reference §14 "Hardcode secrets → environment
    -- variables only"; the same reasoning applies to our own issued secrets).
    key_prefix      text NOT NULL,
    key_hash        text NOT NULL,

    scopes          text[] NOT NULL DEFAULT '{}',
    rate_limit_rpm  integer,

    -- FR-HDL-5: per-session allowance the caller can be told about.
    session_token_allowance bigint,
    session_cost_allowance_usd numeric(10, 4),

    expires_at      timestamptz,
    revoked_at      timestamptz,
    last_used_at    timestamptz,

    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT api_credentials_prefix_unique UNIQUE (key_prefix)
);

COMMENT ON TABLE api_credentials IS
    'Credentials for the REST and headless (MCP/A2A) surfaces. org_id is the authoritative tenant for every request made with the credential (FR-HDL-3).';
COMMENT ON COLUMN api_credentials.key_prefix IS
    'First few plaintext characters, stored to make lookup a single indexed probe before the hash compare.';

CREATE INDEX api_credentials_org_idx ON api_credentials (org_id) WHERE revoked_at IS NULL;

CREATE TRIGGER api_credentials_touch BEFORE UPDATE ON api_credentials
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Traveler preferences (PRD §7.1: "holds preferences (plans, budget, loyalty)")
--
-- Split deliberately into three shapes rather than one JSON blob:
--   · traveler_profiles       — the few fields the planner reads on every run
--   · traveler_preferences    — open key/value set, one row per preference
--   · loyalty_memberships     — relational because it joins to providers
-- The planner needs profiles fast and unambiguously; the long tail of soft
-- preferences changes shape as the product learns, so it stays schemaless.

CREATE TABLE traveler_profiles (
    user_id             uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    home_city           text,
    home_airport_iata   char(3),
    preferred_currency  char(3),

    -- graph/state.py TripConstraints.sort_preference — the traveler's standing
    -- default, overridable per trip.
    default_sort_preference text NOT NULL DEFAULT 'cost',
    default_cabin_class     text,
    seat_preference         text,
    dietary_requirements    text[] NOT NULL DEFAULT '{}',
    accessibility_needs     text[] NOT NULL DEFAULT '{}',

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT traveler_profiles_iata CHECK (home_airport_iata IS NULL OR home_airport_iata ~ '^[A-Z]{3}$'),
    CONSTRAINT traveler_profiles_sort CHECK (default_sort_preference IN ('cost', 'comfort', 'time', 'rating', 'match')),
    CONSTRAINT traveler_profiles_currency CHECK (preferred_currency IS NULL OR preferred_currency ~ '^[A-Z]{3}$')
);

COMMENT ON TABLE traveler_profiles IS
    'Hot path preferences the planner reads on every run. One row per traveler; the long tail lives in traveler_preferences.';

CREATE INDEX traveler_profiles_org_idx ON traveler_profiles (org_id);

CREATE TRIGGER traveler_profiles_touch BEFORE UPDATE ON traveler_profiles
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE traveler_preferences (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    key         text NOT NULL,
    value       jsonb NOT NULL,

    -- Where the preference came from. Learned preferences are advisory; stated
    -- ones are not, and the planner needs to tell them apart before it silently
    -- narrows a search on a guess.
    source      text NOT NULL DEFAULT 'stated',
    confidence  numeric(3, 2),
    last_seen_at timestamptz,

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT traveler_preferences_unique UNIQUE (user_id, key),
    CONSTRAINT traveler_preferences_source CHECK (source IN ('stated', 'learned', 'imported')),
    CONSTRAINT traveler_preferences_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

COMMENT ON TABLE traveler_preferences IS
    'Open preference set surfaced to the memory/vector store (PRD §5 "memory / vector store (preference retrieval)").';
COMMENT ON COLUMN traveler_preferences.source IS
    'stated preferences are binding; learned ones are advisory and carry a confidence the planner can threshold on.';

CREATE INDEX traveler_preferences_org_key_idx ON traveler_preferences (org_id, key);

CREATE TRIGGER traveler_preferences_touch BEFORE UPDATE ON traveler_preferences
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Consent grants (PRD §7.1, §10.2) ─────────────────────────────────────────

-- "Consent Grant: Attached to a traveler; governs data use and access" and
-- "Consent grants and corporate policy enforced before handoff."
CREATE TABLE consent_grants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    purpose         text NOT NULL,
    scope           text[] NOT NULL DEFAULT '{}',
    state           consent_state NOT NULL DEFAULT 'granted',

    granted_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz,
    revoked_at      timestamptz,

    -- Evidence, not decoration: a consent record that cannot say how it was
    -- obtained is not usable in an audit.
    granted_via     delivery_surface NOT NULL,
    evidence_ref    text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT consent_grants_revoked_coherent
        CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);

COMMENT ON TABLE consent_grants IS
    'Per-traveler data use and access grants. Checked before handoff (PRD §10.2).';
COMMENT ON COLUMN consent_grants.purpose IS
    'What the grant permits, e.g. share_profile_with_provider, retain_price_history, personalize_from_history.';

CREATE INDEX consent_grants_active_idx ON consent_grants (user_id, purpose)
    WHERE state = 'granted';

CREATE TRIGGER consent_grants_touch BEFORE UPDATE ON consent_grants
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
