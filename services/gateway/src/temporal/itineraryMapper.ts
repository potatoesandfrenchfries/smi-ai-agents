/**
 * ItineraryWorkflow.current_itinerary() (see itinerary_workflow.py) returns
 * the Python ItineraryResult dataclass verbatim over Temporal's wire
 * protocol — snake_case field names matching
 * activities/travel_activities.py::ItineraryResult and
 * graph/itinerary_graph.py::compile_itinerary's segment/dining_options
 * dicts exactly. It is NOT a Pydantic model and carries no camelCase
 * aliasing, unlike the conversation API's StructuredResponse contract.
 *
 * This is the one place that translation happens, so route handlers and
 * the frontend never have to know the raw snake_case shape. Fields the
 * backend genuinely doesn't produce per-segment (a structured
 * start/end time, a numeric confidence score, a named booking-provider
 * distinct from the airline/hotel itself) are left undefined rather than
 * fabricated — see conversation: the previous ItineraryView contract
 * silently assumed fields that never existed on the wire.
 */

export type PolicyDecision = "compliant" | "breach" | "waived" | "not_applicable" | "pending";

interface RawSegment {
  type: string;
  segment_id: string;
  candidate_id?: string | null;
  provider?: string | null;
  summary?: string | null;
  price_gbp?: number | null;
  reason?: string | null;
  provenance?: string[] | null;
  handoff_link?: string | null;
  rank_arm?: string | null;
}

interface RawDiningOption {
  id: string;
  name?: string | null;
  cuisine?: string | null;
  avg_spend_per_person_gbp?: number | null;
  reason?: string | null;
  rank_arm?: string | null;
}

interface RawItineraryResult {
  plan_id: string;
  status: string;
  segments?: RawSegment[] | null;
  dining_options?: RawDiningOption[] | null;
  total_cost_gbp?: number | null;
  policy_status?: string | null;
  assumptions?: string[] | null;
}

export interface MappedSegment {
  id: string;
  candidateId: string | null;
  kind: "flight" | "hotel" | "attraction" | "dining" | string;
  title: string;
  subtitle: string | null;
  amount: number | null;
  currency: string;
  policyDecision: PolicyDecision;
  snapshotId: string | null;
  reason: string | null;
  handoffLink: string | null;
  rankArm: "primitive" | "bandit" | null;
}

export interface MappedItinerary {
  planId: string;
  status: string;
  version: number;
  totalAmount: number;
  currency: string;
  assumptions: string[];
  policyStatus: PolicyDecision;
  requiresApproval: boolean;
  segments: MappedSegment[];
}

function mapSegment(s: RawSegment): MappedSegment {
  return {
    id: s.segment_id,
    candidateId: s.candidate_id ?? null,
    kind: s.type,
    // provider is the airline/hotel's own name (compile_itinerary sets it
    // from best_flight["airline"] / best_hotel["name"]) — the closest
    // thing to a headline this segment has, not a distinct booking-source
    // attribution like the old mock's "Duffel"/"OpenStreetMap Overpass".
    title: s.provider ?? "TBC",
    subtitle: s.summary ?? null,
    amount: s.price_gbp ?? null,
    currency: "GBP",
    // No per-segment policy check exists (policy_status is itinerary-level
    // only) — "not_applicable" is honest; "compliant" would claim a check
    // that never ran on this specific segment.
    policyDecision: "not_applicable",
    // provenance is literally "PriceSnapshot IDs behind this option"
    // (graph/state.py::TaskReply) — the real analogue of a snapshot id.
    snapshotId: s.provenance?.[0] ?? null,
    reason: s.reason ?? null,
    handoffLink: s.handoff_link ?? null,
    rankArm: (s.rank_arm as "primitive" | "bandit" | null) ?? null,
  };
}

function mapDiningOption(d: RawDiningOption): MappedSegment {
  const spend = d.avg_spend_per_person_gbp;
  return {
    id: `dining-${d.id}`,
    candidateId: d.id,
    kind: "dining",
    title: d.name ?? "TBC",
    subtitle: d.cuisine ? `${d.cuisine} cuisine` : null,
    amount: spend ?? null,
    currency: "GBP",
    policyDecision: "not_applicable",
    snapshotId: null,
    reason: d.reason ?? null,
    handoffLink: null,
    rankArm: (d.rank_arm as "primitive" | "bandit" | null) ?? null,
  };
}

export function mapItinerary(raw: unknown): MappedItinerary | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as RawItineraryResult;
  if (!r.plan_id) return null;

  const segments = (r.segments ?? []).map(mapSegment);
  // Only the top dining pick — the same "one candidate shown per section"
  // convention flight/hotel/attraction already follow, not the full
  // dining_options[:3] list compile_itinerary carries for context.
  const topDining = (r.dining_options ?? [])[0];
  if (topDining) segments.push(mapDiningOption(topDining));

  const policyStatus = (r.policy_status as PolicyDecision) ?? "pending";

  return {
    planId: r.plan_id,
    status: r.status,
    version: 1, // not tracked per-revision yet; itinerary.py's "version" field never reaches ItineraryResult
    totalAmount: r.total_cost_gbp ?? 0,
    currency: "GBP",
    assumptions: r.assumptions ?? [],
    policyStatus,
    requiresApproval: policyStatus === "breach",
    segments,
  };
}
