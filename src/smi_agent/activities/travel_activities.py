"""Temporal activity definitions for the Smartinerary itinerary workflow.

Activities are the units of work that do real I/O — HTTP calls, LLM calls,
database writes. Temporal executes each activity at least once, retries on
failure, and records completion so a retried workflow never re-runs a finished
activity.

Each activity receives a typed dataclass input and returns a JSON-serialisable
output. Dataclasses are used (not TypedDicts) because Temporal's data converter
serialises them cleanly via the standard JSON codec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from temporalio import activity

logger = logging.getLogger(__name__)


# ── Input / output dataclasses ────────────────────────────────────────────────
# Temporal serialises these to/from JSON automatically.

@dataclass
class FlightSearchParams:
    origin: str
    destination: str
    date: str
    sort_by: str = "cost"
    num_results: int = 5


@dataclass
class HotelSearchParams:
    location: str
    check_in: str
    check_out: str
    sort_by: str = "rating"
    num_results: int = 5


@dataclass
class RestaurantSearchParams:
    location: str
    cuisine: str | None = None
    sort_by: str = "rating"
    num_results: int = 5


@dataclass
class AttractionSearchParams:
    location: str
    sort_by: str = "rating"
    num_results: int = 5


@dataclass
class TripIntentParams:
    raw_goal: str


@dataclass
class TripIntentResult:
    origin: str | None = None
    destination: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    budget_gbp: float | None = None
    purpose: str = "leisure"
    traveler_count: int = 1
    sort_preference: str = "cost"
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class ItineraryParams:
    plan_id: str
    tenant_id: str
    raw_goal: str
    # Empty when the caller has no traveler identity (e.g. scripts/demo.py) —
    # ranking then falls back to the primitive arm (see rank_candidates).
    user_id: str = ""
    origin: str = ""
    destination: str = ""
    check_in: str = ""
    check_out: str = ""
    budget_gbp: float | None = None
    sort_by: str = "cost"
    flights: list[dict] = field(default_factory=list)
    hotels: list[dict] = field(default_factory=list)
    restaurants: list[dict] = field(default_factory=list)
    attractions: list[dict] = field(default_factory=list)
    # Agent-to-agent handoff (FR-ORC-4): the flight specialist's resolved
    # arrival time and the hotel specialist's resolved name, threaded through
    # from ItineraryWorkflow's sequential search stage so this activity can
    # attach the same cross-segment feasibility/location notes the graph's
    # own run_hotel_search/run_restaurant_search would (see
    # smi_agent.graph.itinerary_graph.checkin_feasibility_note/near_hotel_note).
    flight_arrival: str | None = None
    hotel_name: str | None = None
    # HITL edit re-runs (FR-PRS-2): when set, skip_reparse tells parse_intent to
    # trust resolved_constraints as-is (e.g. an edited budget_gbp) instead of
    # re-deriving everything from raw_goal, which would otherwise silently
    # discard the edit and waste an LLM call.
    resolved_constraints: dict | None = None
    skip_reparse: bool = False


@dataclass
class ItineraryResult:
    plan_id: str
    status: str                          # "awaiting_confirmation" | "error"
    segments: list[dict] = field(default_factory=list)
    dining_options: list[dict] = field(default_factory=list)
    total_cost_gbp: float | None = None
    policy_status: str = "pending"
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_alternatives: list[dict] = field(default_factory=list)  # Budget Agent output on breach
    resolved_constraints: dict = field(default_factory=dict)  # What parse_intent resolved — carried forward for HITL edits
    quality_review: dict = field(default_factory=dict)  # reflect_itinerary's critic findings
    decision_log: list[dict] = field(default_factory=list)  # Selected vs. rejected candidates per section, with reasons


@dataclass
class PersistTripParams:
    trip_id: str
    user_id: str
    tenant_id: str
    status: str
    origin: str
    destination: str
    check_in: str
    check_out: str
    segments: list[dict] = field(default_factory=list)
    dining_options: list[dict] = field(default_factory=list)
    total_cost_gbp: float | None = None
    policy_status: str = "pending"
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_alternatives: list[dict] = field(default_factory=list)


# ── Activities ────────────────────────────────────────────────────────────────

@activity.defn
async def flight_search_activity(params: FlightSearchParams) -> list[dict]:
    """Search for available flights. Retried by Temporal on failure."""
    from smi_agent.mcp_client.client import get_mcp_client
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("flight_search"):
        activity.logger.info(
            "Searching flights %s → %s on %s", params.origin, params.destination, params.date
        )
        results = await get_mcp_client().call_tool("search_flights", {
            "origin": params.origin,
            "destination": params.destination,
            "date": params.date,
            "sort_by": params.sort_by,
            "num_results": params.num_results,
        })
        activity.logger.info("Flight search returned %d results", len(results))
        return results


@activity.defn
async def hotel_search_activity(params: HotelSearchParams) -> list[dict]:
    """Search for available hotels. Retried by Temporal on failure."""
    from smi_agent.mcp_client.client import get_mcp_client
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("hotel_search"):
        activity.logger.info(
            "Searching hotels in %s (%s to %s)", params.location, params.check_in, params.check_out
        )
        results = await get_mcp_client().call_tool("search_hotels", {
            "location": params.location,
            "check_in": params.check_in,
            "check_out": params.check_out,
            "sort_by": params.sort_by,
            "num_results": params.num_results,
        })
        activity.logger.info("Hotel search returned %d results", len(results))
        return results


@activity.defn
async def restaurant_search_activity(params: RestaurantSearchParams) -> list[dict]:
    """Search for restaurants. Retried by Temporal on failure."""
    from smi_agent.mcp_client.client import get_mcp_client
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("restaurant_search"):
        activity.logger.info("Searching restaurants in %s", params.location)
        results = await get_mcp_client().call_tool("search_restaurants", {
            "location": params.location,
            "cuisine": params.cuisine,
            "sort_by": params.sort_by,
            "num_results": params.num_results,
        })
        activity.logger.info("Restaurant search returned %d results", len(results))
        return results


@activity.defn
async def attraction_search_activity(params: AttractionSearchParams) -> list[dict]:
    """Search for tourist attractions and experiences. Retried by Temporal on failure.

    Always dispatched alongside the other three searches (cheap mock data, no
    real cost) — whether it ends up shown to the traveler depends on the
    business/leisure classification the graph makes downstream.
    """
    from smi_agent.examples.travel.tools.attraction_scraper import search_attractions
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("attraction_search"):
        activity.logger.info("Searching attractions in %s", params.location)
        results = await search_attractions(
            location=params.location,
            sort_by=params.sort_by,
            num_results=params.num_results,
        )
        activity.logger.info("Attraction search returned %d results", len(results))
        return results


@activity.defn
async def parse_trip_intent_activity(params: TripIntentParams) -> TripIntentResult:
    """Extract structured trip constraints from a free-text goal.

    ItineraryWorkflow calls this first when the caller (e.g. the gateway's
    POST /api/v1/trips, which only collects free text) didn't supply
    pre-resolved origin/destination/check_in/check_out — the search
    activities below need those before they can run. Reuses the exact same
    LLM+regex extraction the LangGraph's parse_intent node uses (see
    resolve_trip_constraints in graph/itinerary_graph.py), so there is one
    source of truth for turning raw_goal into TripConstraints.
    """
    from smi_agent.graph.itinerary_graph import resolve_trip_constraints
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("parse_trip_intent"):
        activity.logger.info("Parsing trip intent from raw_goal=%r", params.raw_goal[:160])
        constraints, missing = await resolve_trip_constraints(params.raw_goal)
        activity.logger.info(
            "Trip intent parsed: origin=%s destination=%s check_in=%s check_out=%s missing=%s",
            constraints.get("origin"), constraints.get("destination"),
            constraints.get("check_in"), constraints.get("check_out"), missing,
        )
        return TripIntentResult(
            origin=constraints.get("origin"),
            destination=constraints.get("destination"),
            check_in=constraints.get("check_in"),
            check_out=constraints.get("check_out"),
            budget_gbp=constraints.get("budget_gbp"),
            purpose=constraints.get("purpose") or "leisure",
            traveler_count=constraints.get("traveler_count") or 1,
            sort_preference=constraints.get("sort_preference") or "cost",
            missing_fields=missing,
        )


@activity.defn
async def itinerary_generation_activity(params: ItineraryParams) -> ItineraryResult:
    """Compile the final itinerary from all search results via the LangGraph workflow.

    Runs the LangGraph itinerary graph with pre-fetched search results injected
    into state. search_business_specialists/search_leisure_specialists reuse
    any reply already present in state instead of re-fetching it, so this data
    isn't silently discarded (FR-ORC-3 — no redundant re-fetch of what's
    already known).
    """
    from smi_agent.observability.metrics import track_agent_execution

    with track_agent_execution("itinerary_generation"):
        return await _generate_itinerary(params)


async def _generate_itinerary(params: ItineraryParams) -> ItineraryResult:
    import os
    import uuid

    from smi_agent.graph.itinerary_graph import (
        build_itinerary_graph,
        checkin_feasibility_note,
        near_hotel_note,
    )
    from smi_agent.graph.state import TaskReply
    from smi_agent.providers.ranking import FileRankingStore, rank_candidates

    activity.logger.info("Generating itinerary for plan %s", params.plan_id)

    graph = build_itinerary_graph()

    # Personalized ranking (providers/ranking/): each user_id is
    # deterministically assigned to the primitive or bandit arm via
    # SMI_RANKING_ROLLOUT_PCT (0-100, default 0 = fully primitive until
    # explicitly rolled out). Falls back to primitive automatically when
    # user_id is empty (e.g. scripts/demo.py has no traveler identity).
    ranking_store = FileRankingStore()
    rollout_pct = float(os.environ.get("SMI_RANKING_ROLLOUT_PCT", "0"))

    flight_sort_by = params.sort_by
    hotel_sort_by = "rating" if params.sort_by == "comfort" else "price"
    attraction_sort_by = "price" if params.sort_by == "cost" else "rating"

    flights, flight_arm = await rank_candidates(
        params.flights, sort_by=flight_sort_by, price_field="price_gbp",
        user_id=params.user_id, store=ranking_store, rollout_pct=rollout_pct,
    )
    hotels, hotel_arm = await rank_candidates(
        params.hotels, sort_by=hotel_sort_by, price_field="total_price_gbp",
        rating_field="rating", proximity_field="distance_from_centre_km",
        categorical_fields={"lodging_type": "lodging_type"},
        user_id=params.user_id, store=ranking_store, rollout_pct=rollout_pct,
    )
    restaurants, restaurant_arm = await rank_candidates(
        params.restaurants, sort_by="rating", price_field="avg_spend_per_person_gbp", rating_field="rating",
        categorical_fields={"cuisine": "cuisine"},
        user_id=params.user_id, store=ranking_store, rollout_pct=rollout_pct,
    )
    attractions, attraction_arm = await rank_candidates(
        params.attractions, sort_by=attraction_sort_by, price_field="entry_fee_gbp", rating_field="rating",
        categorical_fields={"attraction_type": "attraction_type"},
        user_id=params.user_id, store=ranking_store, rollout_pct=rollout_pct,
    )
    activity.logger.info(
        "Ranking arms for plan %s — flight=%s hotel=%s restaurant=%s attraction=%s",
        params.plan_id, flight_arm, hotel_arm, restaurant_arm, attraction_arm,
    )

    # Agent-to-agent handoff (FR-ORC-4): the flight/hotel data
    # ItineraryWorkflow resolved sequentially, surfaced as assumptions here —
    # same wording the graph's own run_hotel_search/run_restaurant_search use.
    hotel_assumptions = ["Double room assumed"]
    if (note := checkin_feasibility_note(params.flight_arrival)):
        hotel_assumptions.append(note)
    near_hotel = near_hotel_note(params.hotel_name)

    # Build TaskReply envelopes from pre-fetched results so the graph can
    # merge and compile without re-running the search nodes.
    flight_reply = TaskReply(
        status="done" if flights else "partial",
        candidates=flights,
        assumptions=["Economy class assumed"],
        cost_usd=0.0,
        confidence=0.9 if flights else 0.4,
        provenance=[f.get("id", str(uuid.uuid4())) for f in flights],
    )
    hotel_reply = TaskReply(
        status="done" if hotels else "partial",
        candidates=hotels,
        assumptions=hotel_assumptions,
        cost_usd=0.0,
        confidence=0.9 if hotels else 0.4,
        provenance=[h.get("id", str(uuid.uuid4())) for h in hotels],
    )
    restaurant_reply = TaskReply(
        status="done" if restaurants else "partial",
        candidates=restaurants,
        assumptions=["Dinner assumed"] + ([near_hotel] if near_hotel else []),
        cost_usd=0.0,
        confidence=0.9 if restaurants else 0.4,
        provenance=[r.get("id", str(uuid.uuid4())) for r in restaurants],
    )
    attraction_reply = TaskReply(
        status="done" if attractions else "partial",
        candidates=attractions,
        assumptions=["Half-day sightseeing pace assumed unless specified"] + ([near_hotel] if near_hotel else []),
        cost_usd=0.0,
        confidence=0.9 if attractions else 0.4,
        provenance=[a.get("id", str(uuid.uuid4())) for a in attractions],
    )

    constraints_payload = (
        params.resolved_constraints
        if params.skip_reparse and params.resolved_constraints
        else {
            "origin": params.origin,
            "destination": params.destination,
            "check_in": params.check_in,
            "check_out": params.check_out,
            "budget_gbp": params.budget_gbp,
            "purpose": "leisure",
            "traveler_count": 1,
            "sort_preference": params.sort_by,
        }
    )

    # Pass structured constraints and pre-fetched replies directly so the
    # graph's search_specialists node uses them rather than re-fetching.
    result = await graph.ainvoke({
        "plan_id": params.plan_id,
        "tenant_id": params.tenant_id,
        "raw_goal": params.raw_goal,
        "flight_reply": flight_reply,
        "hotel_reply": hotel_reply,
        "restaurant_reply": restaurant_reply,
        "attraction_reply": attraction_reply,
        "constraints": constraints_payload,
        "skip_reparse": params.skip_reparse,
        "needs_input": [],
    })

    itin = result.get("itinerary") or {}
    policy = result.get("policy_status", "pending")

    # When policy is breach the graph exits before compile_itinerary runs,
    # so itin is empty. Use "needs_approval" instead of the misleading "error".
    status = "needs_approval" if policy == "breach" and not itin else itin.get("status", "error")

    return ItineraryResult(
        plan_id=params.plan_id,
        status=status,
        segments=itin.get("segments", []),
        dining_options=itin.get("dining_options", []),
        total_cost_gbp=result.get("total_cost_gbp"),
        policy_status=policy,
        assumptions=itin.get("assumptions", []),
        errors=result.get("errors") or [],
        budget_alternatives=result.get("budget_alternatives") or [],
        resolved_constraints=result.get("constraints") or {},
        quality_review=itin.get("quality_review") or {},
        # Read off top-level state (not `itin`) so a budget breach — where
        # compile_itinerary never runs and itin is empty — still surfaces the
        # merge_results/budget_agent decision-log entries.
        decision_log=result.get("decision_log") or [],
    )


@activity.defn
async def persist_trip_activity(params: PersistTripParams) -> None:
    """Persist a confirmed trip so it can be looked up from a later, unrelated conversation.

    File-backed via FileTripStore today; moving to Postgres later means
    swapping the store instantiated here, not touching the workflow that
    calls this activity.
    """
    from smi_agent.observability.metrics import track_agent_execution
    from smi_agent.trip_store import FileTripStore, TripRecord

    with track_agent_execution("persist_trip"):
        record = TripRecord(
            trip_id=params.trip_id,
            user_id=params.user_id,
            tenant_id=params.tenant_id,
            status=params.status,
            origin=params.origin,
            destination=params.destination,
            check_in=params.check_in,
            check_out=params.check_out,
            segments=params.segments,
            dining_options=params.dining_options,
            total_cost_gbp=params.total_cost_gbp,
            policy_status=params.policy_status,
            assumptions=params.assumptions,
            errors=params.errors,
            budget_alternatives=params.budget_alternatives,
        )
        await FileTripStore().save_trip(record)
        activity.logger.info("Persisted trip %s for user %s", params.trip_id, params.user_id)


@dataclass
class WorkflowMetricParams:
    workflow_name: str
    status: str


@activity.defn
async def record_workflow_metric_activity(params: WorkflowMetricParams) -> None:
    """Increments the workflow-execution-count metric.

    Must be called as an activity, never directly inside workflow.run() —
    Temporal replays workflow code from history, which would double-count a
    plain in-workflow counter.inc() call.
    """
    from smi_agent.observability.metrics import WORKFLOW_EXECUTIONS_TOTAL

    WORKFLOW_EXECUTIONS_TOTAL.labels(workflow=params.workflow_name, status=params.status).inc()


@dataclass
class RankingFeedbackEvent:
    """One accept/reject (optionally rated) signal from the HITL review flow
    — see providers/ranking/models.py::RecommendationEvent, which this becomes.
    """
    section: str  # "flight" | "hotel" | "restaurant" | "attraction"
    candidate_id: str
    action: str  # "accepted" | "rejected"
    arm: str  # "primitive" | "bandit" — whichever ranked this candidate
    features: dict[str, float] = field(default_factory=dict)  # continuous axis scores at decision time
    categorical: dict[str, str] = field(default_factory=dict)  # categorical axis -> tag at decision time
    rating: int | None = None  # 1-5, optional finer-grained signal — see RecommendationEvent


@dataclass
class RankingFeedbackParams:
    user_id: str
    tenant_id: str
    events: list[RankingFeedbackEvent] = field(default_factory=list)


@activity.defn
async def record_ranking_feedback_activity(params: RankingFeedbackParams) -> None:
    """Persist HITL accept/reject events and update the user's learned
    ranking weights from them — the write side of providers/ranking/.

    Two updates per event: update_axis_weights (dense — every axis moves a
    little, continuous axes from their feature score, categorical axes from
    how much we already believed in the tag the candidate had) and, for
    each categorical axis present, update_tag_weight (sparse — only that
    specific tag moves within its axis).

    Must be called as an activity: it does file I/O, which workflow.run()
    (replayed deterministically from history) can never do directly — same
    reason persist_trip_activity and record_workflow_metric_activity are
    activities rather than inline workflow code.
    """
    import uuid

    from smi_agent.observability.metrics import track_agent_execution
    from smi_agent.providers.ranking import (
        FileRankingStore,
        RecommendationEvent,
        categorical_axis_score,
        reward_from_rating,
        update_axis_weights,
        update_tag_weight,
    )

    if not params.events or not params.user_id:
        return

    with track_agent_execution("record_ranking_feedback"):
        store = FileRankingStore()
        weights = await store.get_weights(params.user_id)

        for event in params.events:
            await store.record_event(RecommendationEvent(
                event_id=str(uuid.uuid4()),
                user_id=params.user_id,
                tenant_id=params.tenant_id,
                section=event.section,
                candidate_id=event.candidate_id,
                action=event.action,
                arm=event.arm,
                features=event.features,
                categorical=event.categorical,
                rating=event.rating,
            ))
            # A rating, when present, replaces the coarse ±1.0 action-only
            # reward with the finer -1..+1 scale reward_from_rating gives —
            # a grudging 3-star accept shouldn't move weights as much as an
            # enthusiastic 5-star one (see conversation).
            reward = (
                reward_from_rating(event.rating)
                if event.rating is not None
                else (1.0 if event.action == "accepted" else -1.0)
            )

            axis_scores = dict(event.features)
            for axis, tag in event.categorical.items():
                axis_scores[axis] = categorical_axis_score(weights, axis, tag)
            weights = update_axis_weights(weights, axis_scores, reward)

            for axis, tag in event.categorical.items():
                weights = update_tag_weight(weights, axis, tag, reward)

        await store.save_weights(params.user_id, weights)
        activity.logger.info(
            "Recorded %d ranking feedback event(s) for user %s — axis_weights now %s (n=%d)",
            len(params.events), params.user_id, weights.axis_weights, weights.event_count,
        )

