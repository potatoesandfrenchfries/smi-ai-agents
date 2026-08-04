-- ═════════════════════════════════════════════════════════════════════════════
-- 009 · Reference data — configuration rows the platform cannot run without.
--
-- Everything here is data an operator would otherwise have to enter by hand
-- before the first request could be served. Tenant-specific rows (a real
-- organization, its users, its policy) are deliberately not seeded — those
-- come from onboarding, not a migration.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── Currencies (FR-INT-5) ─────────────────────────────────────────────────────

INSERT INTO currencies (code, display_name, minor_units, symbol) VALUES
    ('GBP', 'Pound Sterling', 2, '£'),
    ('USD', 'US Dollar',      2, '$'),
    ('EUR', 'Euro',           2, '€'),
    ('JPY', 'Japanese Yen',   0, '¥'),
    ('CHF', 'Swiss Franc',    2, 'CHF'),
    ('AUD', 'Australian Dollar', 2, 'A$'),
    ('CAD', 'Canadian Dollar',   2, 'C$'),
    ('SGD', 'Singapore Dollar',  2, 'S$')
ON CONFLICT DO NOTHING;


-- ── Segment types (PRD §7.2) ──────────────────────────────────────────────────

INSERT INTO segment_types (kind, display_name, owning_kind, is_bookable, is_span, sort_order) VALUES
    ('flight',   'Flight',          'flight',   true,  true,  10),
    ('hotel',    'Hotel',           'lodging',  true,  true,  20),
    ('rail',     'Rail',            'ground',   true,  true,  30),
    ('car',      'Car Rental',      'ground',   true,  true,  40),
    ('transfer', 'Ground Transfer', 'ground',   true,  false, 50),
    ('dining',   'Dining',          'dining',   false, false, 60),
    ('activity', 'Activity',        'activity', true,  false, 70)
ON CONFLICT DO NOTHING;


-- ── Specialist roles (PRD §4.3; mirrors agent_definitions/*.yaml) ────────────
--
-- Kept in sync with the YAML files by hand until the loader in
-- config/loader.py grows a --sync-db step; the YAML remains authoritative for
-- prompts, allowlists, and behavior (Design Reference §4.2, §9.1).

INSERT INTO specialist_roles (agent_name, kind, display_name, description, llm_lane, max_tool_iterations, max_llm_calls, max_cost_usd, requires_org_mode) VALUES
    ('specialist_planner',    'planner',    'Planner',    'Decomposes a trip goal and coordinates specialist dispatch.', 'reasoning', 6, 10, 0.2500, NULL),
    ('specialist_flight',     'flight',     'Flight',     'Searches and verifies flight options against flight providers.', 'middle', 5, 8, 0.1500, NULL),
    ('specialist_hotel',      'lodging',    'Hotel',      'Searches and verifies lodging options against hotel providers.', 'middle', 5, 8, 0.1500, NULL),
    ('specialist_restaurant', 'dining',     'Restaurant', 'Searches and ranks dining options against restaurant providers.', 'middle', 5, 8, 0.1500, NULL),
    ('specialist_budget',     'budget',     'Budget',     'Proposes lower-cost alternatives when a plan breaches budget.', 'middle', 4, 6, 0.1000, NULL),
    ('specialist_policy',     'policy',     'Policy & Compliance', 'Flags or blocks non-compliant options in corporate mode (FR-SPC-2).', 'triage', 4, 6, 0.1000, 'corporate')
ON CONFLICT (agent_name) DO NOTHING;


-- ── Provider gateways and providers (PRD §7.2, §8) ───────────────────────────

INSERT INTO provider_gateways (code, kind, display_name, description) VALUES
    ('gds_flight',    'gds',          'Flight GDS Gateway',        'Legacy and API-first flight sources (PRD §8).'),
    ('hotel_gateway',  'hotel',        'Hotel Gateway',              'Lodging search and verification sources.'),
    ('activity_gateway', 'activity',   'Activity Gateway',           'Activity and attraction sources.'),
    ('ground_gateway',  'ground',       'Ground/Rail Gateway',        'Rail, car, and transfer sources.'),
    ('dining_gateway',   'dining',      'Dining Gateway',             'Restaurant sources.'),
    ('geo_routing',      'geo_routing', 'Geo & Routing Service',      'Transfer feasibility and geocoding (PRD §5).'),
    ('currency_service', 'currency',    'Currency Service',           'Multi-currency normalization (FR-INT-5).')
ON CONFLICT DO NOTHING;

INSERT INTO providers (gateway_id, code, display_name, kind, supports_kinds, booking_model, requires_accreditation, snapshot_ttl_seconds)
SELECT g.id, v.code, v.display_name, v.kind::provider_kind, v.supports_kinds::segment_kind[], v.booking_model, v.requires_accreditation, v.ttl
FROM provider_gateways g
JOIN (VALUES
    ('gds_flight',     'amadeus',    'Amadeus',    'gds',        ARRAY['flight'], 'Enterprise (IATA/ARC) + self-service tier', true,  900),
    ('gds_flight',     'sabre',      'Sabre',      'gds',        ARRAY['flight'], 'Enterprise / accreditation',                true,  900),
    ('gds_flight',     'travelport', 'Travelport', 'gds',        ARRAY['flight'], 'Enterprise (Galileo, Worldspan, Apollo)',   true,  900),
    ('gds_flight',     'duffel',     'Duffel',     'api_first',  ARRAY['flight'], 'Per-booking with volume discounts',         false, 300),
    ('gds_flight',     'kiwi',       'Kiwi (Tequila)', 'aggregator', ARRAY['flight'], 'Project-gated by monthly active users', false, 300),
    ('gds_flight',     'skyscanner', 'Skyscanner', 'metasearch', ARRAY['flight'], 'Affiliate / comparison',                    false, 300),
    ('gds_flight',     'aviationstack', 'AviationStack', 'api_first', ARRAY['flight'], 'Read-only schedule/status data',      false, 300),
    ('hotel_gateway',  'overpass_hotel', 'OpenStreetMap Overpass (hotel)', 'open_data', ARRAY['hotel'], 'Search only, no booking API', false, 900),
    ('dining_gateway', 'overpass_dining', 'OpenStreetMap Overpass (dining)', 'open_data', ARRAY['dining'], 'Search only, no booking API', false, 900),
    ('activity_gateway', 'overpass_activity', 'OpenStreetMap Overpass (activity)', 'open_data', ARRAY['activity'], 'Search only, no booking API', false, 1800)
) AS v(gateway_code, code, display_name, kind, supports_kinds, booking_model, requires_accreditation, ttl)
    ON g.code = v.gateway_code
ON CONFLICT (code) DO NOTHING;


-- ── Base constraint definitions (PRD §7.2; graph/state.py TripConstraints) ───

INSERT INTO constraint_definitions (kind, code, display_name, description, is_required, prompt_text, value_schema) VALUES
    ('budget_cap',        'budget_cap',        'Budget',          'Total trip budget cap.',              true,  'What is the total budget for this trip?', '{"type":"object","properties":{"amount":{"type":"number","exclusiveMinimum":0},"currency":{"type":"string"}},"required":["amount","currency"]}'),
    ('time_window',       'travel_dates',      'Travel Dates',    'Check-in and check-out dates.',        true,  'What are your travel dates?', '{"type":"object","properties":{"check_in":{"type":"string","format":"date"},"check_out":{"type":"string","format":"date"}},"required":["check_in","check_out"]}'),
    ('traveler_count',    'traveler_count',    'Traveler Count',  'Number of travelers.',                  false, 'How many travelers?', '{"type":"integer","minimum":1}'),
    ('sort_preference',   'sort_preference',   'Sort Preference', 'cost | comfort | time | rating | match.', false, NULL, '{"type":"string","enum":["cost","comfort","time","rating","match"]}'),
    ('cabin_class',       'cabin_class',       'Cabin Class',     'Preferred flight cabin.',                false, NULL, '{"type":"string","enum":["ECONOMY","PREMIUM_ECONOMY","BUSINESS","FIRST"]}'),
    ('max_stops',         'max_stops',         'Max Stops',       'Maximum flight stops.',                  false, NULL, '{"type":"integer","minimum":0}'),
    ('refundable_only',   'refundable_only',   'Refundable Only', 'Restrict to refundable fares/rates.',   false, NULL, '{"type":"boolean"}'),
    ('policy_rule',       'corporate_policy',  'Corporate Policy', 'Applicable corporate travel policy.',  false, NULL, '{"type":"string"}'),
    ('dietary',           'dietary',           'Dietary Requirements', 'Dietary constraints for dining segments.', false, NULL, '{"type":"array","items":{"type":"string"}}')
ON CONFLICT (code) DO NOTHING;


-- ── Model registry (Design Reference §7.1, §10.3) ────────────────────────────
--
-- Prices are illustrative placeholders — an operator updates these from the
-- provider's published pricing page; nothing in the schema depends on the
-- exact figures being current.
--
-- Cost policy: this project is pinned to Haiku only (see
-- src/smi_agent/llm/providers.py's PROVIDER_MODELS — every lane the router
-- actually uses resolves to the same Haiku model ID). Opus and Sonnet are
-- seeded below as disabled, non-default rows purely for audit/reference —
-- they exist in the registry, but is_enabled=false means they're not a live
-- option, and is_default_for_lane=false on both keeps the one-default-per-lane
-- index pointed at Haiku everywhere.

INSERT INTO model_registry (lane, provider, model_id, input_cost_per_mtok, output_cost_per_mtok, context_window, is_default_for_lane, is_enabled) VALUES
    ('reasoning', 'anthropic', 'claude-opus-5',    15.00, 75.00, 1000000, false, false),
    ('middle',    'anthropic', 'claude-sonnet-5',   3.00, 15.00, 1000000, false, false),
    ('reasoning', 'anthropic', 'claude-haiku-4-5', 0.80,  4.00,  200000,  true,  true),
    ('middle',    'anthropic', 'claude-haiku-4-5', 0.80,  4.00,  200000,  true,  true),
    ('triage',    'anthropic', 'claude-haiku-4-5', 0.80,  4.00,  200000,  true,  true),
    ('air_gap',   'local',     'llama-3.1-70b-instruct', 0.00, 0.00, 128000, true, true)
ON CONFLICT (lane, provider, model_id) DO NOTHING;
