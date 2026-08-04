-- ═════════════════════════════════════════════════════════════════════════════
-- 003 · Configured entities (PRD §7.2) and the shared lookup services (PRD §5).
--
-- The PRD draws a hard line between "what is requested" (a traveler's plan) and
-- "what is configured" (roles, segment types, constraints, gateways an operator
-- sets up). This file is the configured half. It is read-mostly, written by
-- platform operators, and safe to cache aggressively.
--
-- Note on agent definitions: the YAML files under agent_definitions/ remain the
-- source of truth for prompts, allowlists, and budgets (Design Reference §9.1 —
-- "No hardcoded behavior", and §13 — a new specialist is a YAML file plus a
-- restart). specialist_roles below is the database's *projection* of that
-- registry, so plans and tasks can carry a foreign key instead of a loose
-- string, and so an operator can enable or disable a role per tenant without
-- editing YAML.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── Specialist roles (PRD §7.2 "Flight planner, hotel planner, activity planner, …")

CREATE TABLE specialist_roles (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Matches the ``name`` field of an agent_definitions/*.yaml file, which is
    -- how the AgentRegistry discovers it at startup.
    agent_name          text NOT NULL UNIQUE,
    kind                specialist_kind NOT NULL,
    display_name        text NOT NULL,
    description         text NOT NULL,

    -- Design Reference §7.1: agents declare a lane, not a model.
    llm_lane            llm_lane NOT NULL DEFAULT 'middle',

    -- Design Reference §3.2 budget triple, mirrored here so an operator can see
    -- and audit every agent's ceiling in one query.
    max_tool_iterations integer NOT NULL DEFAULT 5,
    max_llm_calls       integer NOT NULL DEFAULT 8,
    max_cost_usd        numeric(8, 4) NOT NULL DEFAULT 0.1500,

    -- PRD §2: the policy specialist runs in corporate mode only.
    requires_org_mode   org_mode,
    is_enabled          boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT specialist_roles_budget_positive
        CHECK (max_tool_iterations > 0 AND max_llm_calls > 0 AND max_cost_usd > 0)
);

COMMENT ON TABLE specialist_roles IS
    'Database projection of the YAML agent registry. YAML stays authoritative for behavior; this table gives plans a foreign key and operators a per-tenant kill switch.';
COMMENT ON COLUMN specialist_roles.requires_org_mode IS
    'NULL means the role runs in every mode. Set to corporate for the policy and compliance specialist (PRD §2).';

CREATE TRIGGER specialist_roles_touch BEFORE UPDATE ON specialist_roles
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE org_specialist_bindings (
    org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role_id     uuid NOT NULL REFERENCES specialist_roles(id) ON DELETE CASCADE,
    is_enabled  boolean NOT NULL DEFAULT true,

    -- A tenant may buy a tighter budget than the platform default, never a
    -- looser one; the API enforces the direction, the column just records it.
    max_cost_usd_override numeric(8, 4),

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, role_id)
);

COMMENT ON TABLE org_specialist_bindings IS
    'Per-tenant enable/disable and budget override for a specialist role (FR-HDL-7 generalized beyond the headless surface).';

CREATE TRIGGER org_specialist_bindings_touch BEFORE UPDATE ON org_specialist_bindings
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Segment types (PRD §7.2) ─────────────────────────────────────────────────

CREATE TABLE segment_types (
    kind                segment_kind PRIMARY KEY,
    display_name        text NOT NULL,

    -- Which specialist owns the search for this segment type. One row per kind
    -- keeps the orchestrator's decomposition table-driven rather than coded.
    owning_kind         specialist_kind NOT NULL,

    -- Whether a segment of this kind can be handed off for booking at all.
    -- Dining and activity segments are frequently informational.
    is_bookable         boolean NOT NULL DEFAULT true,

    -- Whether a segment of this kind occupies a time span (flight, hotel night)
    -- or a point in time (dinner reservation). Drives cross-segment feasibility
    -- checks in the merge step (FR-ORC-4).
    is_span             boolean NOT NULL DEFAULT true,

    sort_order          integer NOT NULL DEFAULT 100
);

COMMENT ON TABLE segment_types IS
    'PRD §7.2 configured segment types. Table-driven so the orchestrator decomposition and the UI ordering come from data.';


-- ── Constraint definitions (PRD §7.2 "Budget cap, time window, policy rule, …")

CREATE TABLE constraint_definitions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            constraint_kind NOT NULL,
    code            text NOT NULL UNIQUE,
    display_name    text NOT NULL,
    description     text NOT NULL,

    -- FR-INT-2 validates a Trip's constraint set for completeness before
    -- planning starts, and FR-INT-3 prompts rather than guessing. These two
    -- columns are what make that check data-driven.
    is_required     boolean NOT NULL DEFAULT false,
    prompt_text     text,

    -- JSON Schema fragment the supplied value must satisfy. Validation happens
    -- in the API layer; storing the schema here keeps one definition of "valid"
    -- rather than one per surface (FR-INT-4).
    value_schema    jsonb NOT NULL DEFAULT '{}'::jsonb,

    applies_to_kinds segment_kind[] NOT NULL DEFAULT '{}',

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT constraint_definitions_prompt_present
        CHECK (NOT is_required OR prompt_text IS NOT NULL)
);

COMMENT ON TABLE constraint_definitions IS
    'Catalogue of constraint kinds a Trip may carry. is_required + prompt_text make FR-INT-2 completeness checks and FR-INT-3 prompts data-driven.';
COMMENT ON CONSTRAINT constraint_definitions_prompt_present ON constraint_definitions IS
    'A required constraint with no prompt text would leave FR-INT-3 with nothing to ask.';

CREATE TRIGGER constraint_definitions_touch BEFORE UPDATE ON constraint_definitions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Corporate policy (FR-SPC-2, FR-PRS-4) ────────────────────────────────────

CREATE TABLE policies (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    name            text NOT NULL,
    description     text,

    -- Policies are versioned rather than edited in place: an itinerary approved
    -- last month must still be explainable against the policy that approved it.
    version         integer NOT NULL DEFAULT 1,
    is_active       boolean NOT NULL DEFAULT true,
    effective_from  timestamptz NOT NULL DEFAULT now(),
    effective_to    timestamptz,

    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT policies_name_version_unique UNIQUE (org_id, name, version),
    CONSTRAINT policies_effective_range CHECK (effective_to IS NULL OR effective_to > effective_from)
);

COMMENT ON TABLE policies IS
    'Versioned per-tenant policy set. Never edited in place so a historical approval stays explainable.';

CREATE INDEX policies_org_active_idx ON policies (org_id) WHERE is_active;

CREATE TRIGGER policies_touch BEFORE UPDATE ON policies
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE policy_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id       uuid NOT NULL REFERENCES policies(id) ON DELETE CASCADE,

    code            text NOT NULL,
    title           text NOT NULL,
    applies_to      segment_kind,

    -- Declarative predicate the policy specialist evaluates. Kept as a
    -- structured document rather than an expression string: Design Reference §14
    -- rules out letting a model author executable logic, and the same caution
    -- applies to operator-authored logic reaching an evaluator.
    predicate       jsonb NOT NULL,

    -- 'flags' surfaces a warning; 'blocks' makes the option non-selectable
    -- (FR-SPC-2, verbatim: "flags or blocks non-compliant options").
    enforcement     text NOT NULL DEFAULT 'flags',

    -- FR-PRS-4: breaching this rule routes the plan to an approver.
    requires_approval boolean NOT NULL DEFAULT false,
    approver_role   org_role NOT NULL DEFAULT 'approver',

    message         text NOT NULL,
    sort_order      integer NOT NULL DEFAULT 100,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT policy_rules_code_unique UNIQUE (policy_id, code),
    CONSTRAINT policy_rules_enforcement CHECK (enforcement IN ('flags', 'blocks'))
);

COMMENT ON TABLE policy_rules IS
    'Individual rules within a policy version. enforcement=blocks makes an option non-selectable; requires_approval routes the plan (FR-SPC-2, FR-PRS-4).';
COMMENT ON COLUMN policy_rules.predicate IS
    'Structured, non-executable condition document, e.g. {"field":"cabin_class","op":"in","value":["ECONOMY","PREMIUM_ECONOMY"]}.';

CREATE INDEX policy_rules_policy_idx ON policy_rules (policy_id, sort_order);

CREATE TRIGGER policy_rules_touch BEFORE UPDATE ON policy_rules
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Provider gateways and providers (PRD §7.2, §8) ───────────────────────────

CREATE TABLE provider_gateways (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL UNIQUE,
    kind            gateway_kind NOT NULL,
    display_name    text NOT NULL,
    description     text,
    is_enabled      boolean NOT NULL DEFAULT true,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE provider_gateways IS
    'PRD §7.2 provider gateways. A gateway groups providers that share an adapter shape, so a provider can be swapped without touching the A2A contract (FR-SPC-6).';

CREATE TRIGGER provider_gateways_touch BEFORE UPDATE ON provider_gateways
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE providers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    gateway_id          uuid NOT NULL REFERENCES provider_gateways(id) ON DELETE RESTRICT,

    -- Matches the registry key in src/smi_agent/providers/registry.py, so the
    -- env var SMI_FLIGHT_PROVIDER=duffel resolves to this row.
    code                text NOT NULL UNIQUE,
    display_name        text NOT NULL,
    kind                provider_kind NOT NULL,
    supports_kinds      segment_kind[] NOT NULL DEFAULT '{}',

    -- PRD §8 "Booking model" column.
    booking_model       text,

    -- PRD §12 open question: "Provider accreditation requirements (e.g.
    -- IATA/ARC) gating which flight sources are live per customer."
    requires_accreditation boolean NOT NULL DEFAULT false,
    accreditation_note  text,

    -- FR-EVT-4 / §10.1: TTL-keyed inventory caching. This is the default TTL
    -- for snapshots taken from this provider, and the floor for how long a
    -- quoted price may be presented as current.
    snapshot_ttl_seconds integer NOT NULL DEFAULT 900,
    rate_limit_rpm      integer,

    is_enabled          boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT providers_ttl_positive CHECK (snapshot_ttl_seconds > 0)
);

COMMENT ON TABLE providers IS
    'External sources a segment is fulfilled against (PRD §7.1, §8). code matches the key in providers/registry.py.';
COMMENT ON COLUMN providers.snapshot_ttl_seconds IS
    'Default TTL for PriceSnapshots from this provider. The UI shows remaining life so a reviewer knows whether a quote is still current.';

CREATE INDEX providers_gateway_idx ON providers (gateway_id) WHERE is_enabled;

CREATE TRIGGER providers_touch BEFORE UPDATE ON providers
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE org_provider_bindings (
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider_id     uuid NOT NULL REFERENCES providers(id) ON DELETE CASCADE,

    is_enabled      boolean NOT NULL DEFAULT true,

    -- Accreditation is per customer, not per platform (PRD §12).
    accreditation_verified_at timestamptz,

    -- Priority within a gateway when more than one provider can answer. Lower
    -- runs first; ties are broken by provider code for determinism.
    priority        integer NOT NULL DEFAULT 100,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, provider_id)
);

COMMENT ON TABLE org_provider_bindings IS
    'Which providers a tenant may reach, and in what order. Gates accreditation-restricted sources per customer (PRD §12).';

CREATE TRIGGER org_provider_bindings_touch BEFORE UPDATE ON org_provider_bindings
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Loyalty (PRD §7.1: traveler "holds preferences (plans, budget, loyalty)") ─

CREATE TABLE loyalty_memberships (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Programs usually belong to a carrier or chain rather than to the
    -- aggregator we search through, so provider_id is nullable and the free-text
    -- program code is authoritative.
    provider_id         uuid REFERENCES providers(id) ON DELETE SET NULL,
    program_code        text NOT NULL,
    program_name        text NOT NULL,
    membership_number   text NOT NULL,
    tier                text,

    -- The number is traveler PII. Storing it at all requires a live grant, and
    -- this pointer is what an audit follows to prove one existed.
    consent_grant_id    uuid REFERENCES consent_grants(id) ON DELETE SET NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT loyalty_memberships_unique UNIQUE (user_id, program_code)
);

COMMENT ON TABLE loyalty_memberships IS
    'Traveler loyalty programs. consent_grant_id links the stored membership number to the grant that permits holding it (PRD §10.2).';

CREATE INDEX loyalty_memberships_org_idx ON loyalty_memberships (org_id);

CREATE TRIGGER loyalty_memberships_touch BEFORE UPDATE ON loyalty_memberships
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ── Currency service (FR-INT-5, PRD §5) ──────────────────────────────────────

CREATE TABLE currencies (
    code            char(3) PRIMARY KEY,
    display_name    text NOT NULL,
    minor_units     smallint NOT NULL DEFAULT 2,
    symbol          text,

    CONSTRAINT currencies_iso CHECK (code ~ '^[A-Z]{3}$'),
    CONSTRAINT currencies_minor_units CHECK (minor_units BETWEEN 0 AND 4)
);

COMMENT ON TABLE currencies IS
    'ISO 4217 currencies. minor_units matters because JPY has none and rounding a budget wrongly changes a compliance answer.';


CREATE TABLE fx_rates (
    base_currency   char(3) NOT NULL REFERENCES currencies(code),
    quote_currency  char(3) NOT NULL REFERENCES currencies(code),
    rate            numeric(18, 8) NOT NULL,

    -- Rates are point-in-time. A plan normalizes with the rate that was current
    -- when it was priced, so a later rate move cannot retroactively make a
    -- compliant plan non-compliant.
    as_of           timestamptz NOT NULL,
    source          text NOT NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (base_currency, quote_currency, as_of),
    CONSTRAINT fx_rates_positive CHECK (rate > 0),
    CONSTRAINT fx_rates_distinct CHECK (base_currency <> quote_currency)
);

COMMENT ON TABLE fx_rates IS
    'Point-in-time conversion rates backing FR-INT-5. Plans record the as_of they used so a rate move cannot rewrite a past compliance decision.';

CREATE INDEX fx_rates_lookup_idx ON fx_rates (base_currency, quote_currency, as_of DESC);


-- ── Prompt and model registry (PRD §5, §10.3; Design Reference §7, §10.3) ─────

CREATE TABLE model_registry (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    lane                llm_lane NOT NULL,
    provider            text NOT NULL,
    model_id            text NOT NULL,

    -- Cost is per million tokens because that is how providers publish it, and
    -- converting once here beats converting at every call site.
    input_cost_per_mtok  numeric(10, 4),
    output_cost_per_mtok numeric(10, 4),
    context_window      integer,

    is_default_for_lane boolean NOT NULL DEFAULT false,
    is_enabled          boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT model_registry_unique UNIQUE (lane, provider, model_id)
);

COMMENT ON TABLE model_registry IS
    'Versioned lane-to-model mapping with published prices (Design Reference §7.1, §10.3). Central control over model selection and cost.';

-- One default per lane, enforced rather than assumed.
CREATE UNIQUE INDEX model_registry_one_default_per_lane
    ON model_registry (lane) WHERE is_default_for_lane;

CREATE TRIGGER model_registry_touch BEFORE UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE prompt_registry (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Path relative to prompts/, e.g. agents/flight/system.j2.
    template_path   text NOT NULL,
    version         integer NOT NULL DEFAULT 1,

    -- sha256 of the rendered template source. Lets a trace prove which prompt
    -- text produced a given response without storing the text on every call.
    content_hash    text NOT NULL,
    content         text,

    role_id         uuid REFERENCES specialist_roles(id) ON DELETE SET NULL,
    is_active       boolean NOT NULL DEFAULT true,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT prompt_registry_unique UNIQUE (template_path, version),
    CONSTRAINT prompt_registry_hash_format CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMENT ON TABLE prompt_registry IS
    'Versioned prompt templates (PRD §10.3 "Versioned prompt and model registry"). content_hash matches the sha256:<64hex> format used by schemas/plan.py RunMetrics.config_hash.';

CREATE UNIQUE INDEX prompt_registry_one_active_per_path
    ON prompt_registry (template_path) WHERE is_active;
