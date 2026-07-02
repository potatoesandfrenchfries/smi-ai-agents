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
class ItineraryParams:
    plan_id: str
    tenant_id: str
    raw_goal: str
    origin: str = ""
    destination: str = ""
    check_in: str = ""
    check_out: str = ""
    budget_gbp: float | None = None
    sort_by: str = "cost"
    flights: list[dict] = field(default_factory=list)
    hotels: list[dict] = field(default_factory=list)
    restaurants: list[dict] = field(default_factory=list)


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


# ── Activities ────────────────────────────────────────────────────────────────

@activity.defn
async def flight_search_activity(params: FlightSearchParams) -> list[dict]:
    """Search for available flights. Retried by Temporal on failure."""
    from smi_agent.examples.travel.tools.flight_scraper import search_flights

    activity.logger.info(
        "Searching flights %s → %s on %s", params.origin, params.destination, params.date
    )
    results = await search_flights(
        origin=params.origin,
        destination=params.destination,
        date=params.date,
        sort_by=params.sort_by,
        num_results=params.num_results,
    )
    activity.logger.info("Flight search returned %d results", len(results))
    return results


@activity.defn
async def hotel_search_activity(params: HotelSearchParams) -> list[dict]:
    """Search for available hotels. Retried by Temporal on failure."""
    from smi_agent.examples.travel.tools.hotel_scraper import search_hotels

    activity.logger.info(
        "Searching hotels in %s (%s to %s)", params.location, params.check_in, params.check_out
    )
    results = await search_hotels(
        location=params.location,
        check_in=params.check_in,
        check_out=params.check_out,
        sort_by=params.sort_by,
        num_results=params.num_results,
    )
    activity.logger.info("Hotel search returned %d results", len(results))
    return results


@activity.defn
async def restaurant_search_activity(params: RestaurantSearchParams) -> list[dict]:
    """Search for restaurants. Retried by Temporal on failure."""
    from smi_agent.examples.travel.tools.restaurant_scraper import search_restaurants

    activity.logger.info("Searching restaurants in %s", params.location)
    results = await search_restaurants(
        location=params.location,
        cuisine=params.cuisine,
        sort_by=params.sort_by,
        num_results=params.num_results,
    )
    activity.logger.info("Restaurant search returned %d results", len(results))
    return results


@activity.defn
async def itinerary_generation_activity(params: ItineraryParams) -> ItineraryResult:
    """Compile the final itinerary from all search results via the LangGraph workflow.

    Runs the LangGraph itinerary graph with pre-fetched search results injected
    into state, skipping the search nodes and jumping straight to merge → compile.
    """
    import uuid
    from smi_agent.graph.itinerary_graph import build_itinerary_graph
    from smi_agent.graph.state import TaskReply

    activity.logger.info("Generating itinerary for plan %s", params.plan_id)

    graph = build_itinerary_graph()

    # Build TaskReply envelopes from pre-fetched results so the graph can
    # merge and compile without re-running the search nodes.
    flight_reply = TaskReply(
        status="done" if params.flights else "partial",
        candidates=params.flights,
        assumptions=["Economy class assumed"],
        cost_usd=0.0,
        confidence=0.9 if params.flights else 0.4,
        provenance=[f.get("id", str(uuid.uuid4())) for f in params.flights],
    )
    hotel_reply = TaskReply(
        status="done" if params.hotels else "partial",
        candidates=params.hotels,
        assumptions=["Double room assumed"],
        cost_usd=0.0,
        confidence=0.9 if params.hotels else 0.4,
        provenance=[h.get("id", str(uuid.uuid4())) for h in params.hotels],
    )
    restaurant_reply = TaskReply(
        status="done" if params.restaurants else "partial",
        candidates=params.restaurants,
        assumptions=["Dinner assumed"],
        cost_usd=0.0,
        confidence=0.9 if params.restaurants else 0.4,
        provenance=[r.get("id", str(uuid.uuid4())) for r in params.restaurants],
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
        "constraints": {
            "origin": params.origin,
            "destination": params.destination,
            "check_in": params.check_in,
            "check_out": params.check_out,
            "budget_gbp": params.budget_gbp,
            "purpose": "leisure",
            "traveler_count": 1,
            "sort_preference": params.sort_by,
        },
        "needs_input": [],
    })

    itin = result.get("itinerary") or {}
    policy = result.get("policy_status", "pending")

    # When policy is breach the graph exits before compile_itinerary runs,
    # so itin is empty. Use "needs_approval" instead of the misleading "error".
    if policy == "breach" and not itin:
        status = "needs_approval"
    else:
        status = itin.get("status", "error")

    return ItineraryResult(
        plan_id=params.plan_id,
        status=status,
        segments=itin.get("segments", []),
        dining_options=itin.get("dining_options", []),
        total_cost_gbp=result.get("total_cost_gbp"),
        policy_status=policy,
        assumptions=itin.get("assumptions", []),
        errors=result.get("errors") or [],
    )
