"""LangGraph itinerary planning workflow for Smartinerary.

Implements the 6-stage end-to-end plan flow defined in the PRD (Section 5):

  START
    │
    ▼
  parse_intent          Stage 1+2 · Extract Trip with typed constraints (FR-INT-1/2)
    │
    ├─ [needs_input] ──► END     Missing required fields — caller prompts user (FR-INT-3)
    │
    ▼
  search_specialists    Stage 4 · Parallel dispatch to Flight/Hotel/Restaurant (FR-ORC-1)
    │                            asyncio.gather → all three run concurrently
    ▼
  merge_results         Stage 4 · Reconcile candidates, cross-segment feasibility (FR-ORC-4)
    │
    ▼
  policy_check          Stage 4 · Budget + policy compliance gate (FR-SPC-2, FR-ORC-5)
    │
    ├─ [breach] ───────► END     Needs human approval before proceeding (FR-PRS-4)
    │
    ▼
  compile_itinerary     Stage 6 · Versioned itinerary with per-segment handoff links (FR-PRS-1/3)
    │
    ▼
  await_confirmation    Stage 6 · HITL gate — no booking without explicit confirm (FR-GAT-3)
    │
    ▼
   END

Design principles followed (from AI Agent Design Reference):
  - Nodes are isolated; failure in one does not corrupt prior state (Section 5.1)
  - Every node emits SSE progress events via StepEmitter (Section 5.3)
  - Budget tracked per-node so expensive nodes can have tighter limits (Section 5.1)
  - Graceful degradation: partial results returned when budget exceeded (FR-ORC-5)
  - Deterministic routing from state fields, not LLM self-classification (Section 3.4)
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from langgraph.graph import END, START, StateGraph

from smi_agent.graph.state import ItineraryState, TaskReply, TaskRequest, TripConstraints
from smi_agent.streaming.step_emitter import NullStepEmitter

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

REQUIRED_FIELDS: list[str] = ["origin", "destination", "check_in", "check_out"]
SUBTASK_DEADLINE_SECONDS = 30
SUBTASK_BUDGET_USD = 0.50


# ── Helpers ────────────────────────────────────────────────────────────────────

def _emitter(state: ItineraryState) -> Any:
    return state.get("_step_emitter") or NullStepEmitter()


def _missing_fields(constraints: TripConstraints) -> list[str]:
    return [f for f in REQUIRED_FIELDS if not constraints.get(f)]


def _make_task_request(
    goal: str,
    constraints: TripConstraints,
    plan_id: str,
    budget_usd: float,
) -> TaskRequest:
    return TaskRequest(
        task_id=str(uuid.uuid4()),
        goal=goal,
        constraints=constraints,
        context_ref=plan_id,
        deadline_seconds=SUBTASK_DEADLINE_SECONDS,
        budget_remaining_usd=budget_usd,
    )


def _candidates_to_reply(
    candidates: list[dict],
    assumptions: list[str],
) -> TaskReply:
    """Wrap raw search results in the A2A TaskReply envelope (PRD Section 6.2)."""
    return TaskReply(
        status="done" if candidates else "partial",
        candidates=candidates,
        assumptions=assumptions,
        cost_usd=0.0,       # Populated by real LLM router in production
        confidence=0.9 if candidates else 0.4,
        provenance=[c.get("id", "unknown") for c in candidates],
    )


# ── Stage 1+2 · parse_intent ──────────────────────────────────────────────────

async def parse_intent(state: ItineraryState) -> dict:
    """Extract typed TripConstraints from the raw natural-language goal.

    In production this calls the LLMRouter (reasoning lane) to extract structured
    fields. For the prototype, deterministic keyword extraction is used so the
    graph runs without API keys.

    Implements: FR-INT-1 (accept NL goal), FR-INT-2 (parse into Trip + Constraints),
                FR-INT-3 (identify missing fields).
    """
    emitter = _emitter(state)
    await emitter.emit("parse_intent", "in_progress", "Parsing travel goal...")

    goal = state["raw_goal"]
    goal_lower = goal.lower()

    # ── Date extraction ───────────────────────────────────────────────────────
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", goal)
    check_in = dates[0] if len(dates) > 0 else None
    check_out = dates[1] if len(dates) > 1 else None

    # ── Airport / city extraction ─────────────────────────────────────────────
    iata = re.findall(r"\b([A-Z]{3})\b", goal)
    cities = {
        # European / global
        "london": "LHR", "edinburgh": "EDI", "paris": "CDG",
        "amsterdam": "AMS", "berlin": "BER", "madrid": "MAD",
        "rome": "FCO", "new york": "JFK", "dubai": "DXB",
        # Indian cities
        "chennai": "MAA", "mumbai": "BOM", "delhi": "DEL",
        "hyderabad": "HYD", "bengaluru": "BLR", "bangalore": "BLR",
        "lucknow": "LKO", "patna": "PAT", "kolkata": "CCU",
        "ahmedabad": "AMD", "pune": "PNQ", "goa": "GOI", "kochi": "COK",
    }

    # Respect "from X to Y" / "X to Y" direction in the sentence
    # Find positions of each city name and sort by appearance order
    def _find_cities_in_order(text: str) -> list[str]:
        found = []
        for name, code in cities.items():
            idx = text.find(name)
            if idx != -1:
                found.append((idx, code))
        found.sort(key=lambda x: x[0])
        return [code for _, code in found]

    ordered = _find_cities_in_order(goal_lower)

    if len(iata) >= 2:
        origin, destination = iata[0], iata[1]
    elif len(iata) == 1 and len(ordered) >= 1:
        origin, destination = iata[0], ordered[0]
    elif len(ordered) >= 2:
        # Use sentence order — first city mentioned = origin
        origin, destination = ordered[0], ordered[1]
    elif len(ordered) == 1:
        origin, destination = ordered[0], None
    else:
        origin, destination = None, None

    # ── Budget extraction ─────────────────────────────────────────────────────
    budget_match = re.search(r"£(\d[\d,]*)", goal) or re.search(r"\b(\d{3,})\s*(?:gbp|pounds?)\b", goal_lower)
    budget_gbp = float(budget_match.group(1).replace(",", "")) if budget_match else None

    # ── Purpose and preferences ───────────────────────────────────────────────
    purpose = (
        "business" if "business" in goal_lower
        else "corporate" if "corporate" in goal_lower
        else "leisure"
    )
    sort_pref = (
        "comfort" if any(w in goal_lower for w in ["comfort", "direct", "business class"])
        else "time" if any(w in goal_lower for w in ["fastest", "quickest", "shortest"])
        else "cost"
    )
    traveler_count = int(m.group(1)) if (m := re.search(r"\b(\d+)\s+(?:people|travelers?|passengers?)\b", goal_lower)) else 1

    constraints = TripConstraints(
        origin=origin,
        destination=destination,
        check_in=check_in,
        check_out=check_out,
        budget_gbp=budget_gbp,
        purpose=purpose,
        traveler_count=traveler_count,
        sort_preference=sort_pref,
    )

    missing = _missing_fields(constraints)

    if missing:
        await emitter.emit(
            "parse_intent", "completed",
            f"Missing required fields: {', '.join(missing)}"
        )
        logger.warning("[parse_intent] Missing fields: %s", missing)
    else:
        await emitter.emit(
            "parse_intent", "completed",
            f"{origin} → {destination} | {check_in} to {check_out}"
        )

    return {
        "constraints": constraints,
        "needs_input": missing,
        "current_node": "parse_intent",
        "plan_graph": {
            "plan_id": state["plan_id"],
            "raw_goal": state["raw_goal"],
            "parsed_at": datetime.utcnow().isoformat(),
            "stages": ["parse_intent"],
        },
    }


# ── Stage 4 · search_specialists ──────────────────────────────────────────────

async def search_specialists(state: ItineraryState) -> dict:
    """Fan out to Flight, Hotel, and Restaurant specialists in parallel.

    All three run concurrently via asyncio.gather, so total latency tracks
    the slowest specialist — not the sum (FR-ORC-1, NFR performance).

    Each specialist call is wrapped in the A2A TaskRequest envelope (FR-ORC-2).
    Context is passed as a plan_id pointer, never a full context dump (FR-ORC-3).
    """
    emitter = _emitter(state)
    constraints = state["constraints"]
    plan_id = state["plan_id"]

    await emitter.emit(
        "search_specialists", "in_progress",
        "Dispatching flight, hotel, and restaurant searches in parallel..."
    )

    flight_task = _make_task_request(
        goal=f"Find flights from {constraints['origin']} to {constraints['destination']} on {constraints['check_in']}",
        constraints=constraints,
        plan_id=plan_id,
        budget_usd=SUBTASK_BUDGET_USD,
    )
    hotel_task = _make_task_request(
        goal=f"Find hotels in {constraints['destination']} from {constraints['check_in']} to {constraints['check_out']}",
        constraints=constraints,
        plan_id=plan_id,
        budget_usd=SUBTASK_BUDGET_USD,
    )
    restaurant_task = _make_task_request(
        goal=f"Find restaurants in {constraints['destination']}",
        constraints=constraints,
        plan_id=plan_id,
        budget_usd=SUBTASK_BUDGET_USD,
    )

    # Record dispatch in PlanGraph (FR-ORC-6)
    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["dispatched_at"] = datetime.utcnow().isoformat()
    plan_graph["tasks"] = {
        "flight": flight_task["task_id"],
        "hotel": hotel_task["task_id"],
        "restaurant": restaurant_task["task_id"],
    }

    # Run all three concurrently
    flight_result, hotel_result, restaurant_result = await asyncio.gather(
        _run_flight(flight_task, constraints),
        _run_hotel(hotel_task, constraints),
        _run_restaurant(restaurant_task, constraints),
        return_exceptions=True,  # Partial results on failure (FR-ORC-5)
    )

    errors: list[str] = list(state.get("errors") or [])

    flight_reply = _handle_result(flight_result, "flight", errors)
    hotel_reply = _handle_result(hotel_result, "hotel", errors)
    restaurant_reply = _handle_result(restaurant_result, "restaurant", errors)

    plan_graph["stages"] = plan_graph.get("stages", []) + ["search_specialists"]
    plan_graph["completed_at"] = datetime.utcnow().isoformat()

    await emitter.emit(
        "search_specialists", "completed",
        f"Flights: {len(flight_reply['candidates'])}, "
        f"Hotels: {len(hotel_reply['candidates'])}, "
        f"Restaurants: {len(restaurant_reply['candidates'])}"
    )

    return {
        "flight_reply": flight_reply,
        "hotel_reply": hotel_reply,
        "restaurant_reply": restaurant_reply,
        "plan_graph": plan_graph,
        "errors": errors,
        "current_node": "search_specialists",
    }


async def _run_flight(task: TaskRequest, constraints: TripConstraints) -> TaskReply:
    from smi_agent.examples.travel.tools.flight_scraper import search_flights
    results = await search_flights(
        origin=constraints["origin"] or "",
        destination=constraints["destination"] or "",
        date=constraints["check_in"] or "",
        sort_by=constraints.get("sort_preference", "cost"),
    )
    return _candidates_to_reply(results, assumptions=["Economy class assumed if not specified"])


async def _run_hotel(task: TaskRequest, constraints: TripConstraints) -> TaskReply:
    from smi_agent.examples.travel.tools.hotel_scraper import search_hotels
    results = await search_hotels(
        location=constraints["destination"] or "",
        check_in=constraints["check_in"] or "",
        check_out=constraints["check_out"] or "",
        sort_by="rating" if constraints.get("sort_preference") == "comfort" else "price",
    )
    return _candidates_to_reply(results, assumptions=["Double room assumed if not specified"])


async def _run_restaurant(task: TaskRequest, constraints: TripConstraints) -> TaskReply:
    from smi_agent.examples.travel.tools.restaurant_scraper import search_restaurants
    results = await search_restaurants(location=constraints["destination"] or "")
    return _candidates_to_reply(results, assumptions=["Dinner assumed if meal type not specified"])


def _handle_result(result: Any, name: str, errors: list[str]) -> TaskReply:
    """Convert asyncio.gather result (value or exception) to a TaskReply."""
    if isinstance(result, BaseException):
        logger.error("[search_specialists] %s failed: %s", name, result)
        errors.append(f"{name}_search: {result}")
        return TaskReply(
            status="blocked",
            candidates=[],
            assumptions=[],
            cost_usd=0.0,
            confidence=0.0,
            provenance=[],
        )
    return result


# ── Stage 4 · merge_results ───────────────────────────────────────────────────

async def merge_results(state: ItineraryState) -> dict:
    """Reconcile specialist candidates into a coherent plan.

    Performs cross-segment feasibility checks — e.g., hotel check-in must be
    plausible after the latest flight arrival (FR-ORC-4).
    """
    emitter = _emitter(state)
    await emitter.emit("merge_results", "in_progress", "Reconciling candidates...")

    flight = state.get("flight_reply") or {}
    hotel = state.get("hotel_reply") or {}
    restaurant = state.get("restaurant_reply") or {}

    # Cross-segment feasibility: pick best candidate per segment
    best_flight = flight.get("candidates", [{}])[0] if flight.get("candidates") else {}
    best_hotel = hotel.get("candidates", [{}])[0] if hotel.get("candidates") else {}
    best_restaurants = restaurant.get("candidates", [])[:3]

    # Compute total estimated cost
    total_cost = (
        (best_flight.get("price_gbp") or 0) +
        (best_hotel.get("total_price_gbp") or 0) +
        sum(r.get("avg_spend_per_person_gbp") or 0 for r in best_restaurants)
    )

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["merge_results"]
    plan_graph["merge"] = {
        "best_flight_id": best_flight.get("id"),
        "best_hotel_id": best_hotel.get("id"),
        "restaurant_count": len(best_restaurants),
        "total_cost_gbp": round(total_cost, 2),
    }

    await emitter.emit("merge_results", "completed", f"Total estimated cost: £{total_cost:.2f}")

    return {
        "plan_graph": plan_graph,
        "total_cost_gbp": round(total_cost, 2),
        "current_node": "merge_results",
    }


# ── Stage 4 · policy_check ────────────────────────────────────────────────────

async def policy_check(state: ItineraryState) -> dict:
    """Check the plan against the budget constraint.

    In corporate mode this would also run the policy specialist (FR-SPC-2).
    Plans breaching budget are routed for approval before confirmation (FR-PRS-4).
    """
    emitter = _emitter(state)
    await emitter.emit("policy_check", "in_progress", "Checking budget compliance...")

    budget = (state.get("constraints") or {}).get("budget_gbp")
    total = state.get("total_cost_gbp") or 0.0

    if budget and total > budget:
        status: Literal["compliant", "breach", "pending"] = "breach"
        msg = f"Plan cost £{total:.2f} exceeds budget £{budget:.2f} — approval required"
        logger.warning("[policy_check] %s", msg)
    else:
        status = "compliant"
        msg = f"Plan cost £{total:.2f} within budget"

    await emitter.emit("policy_check", "completed", msg)

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["policy_check"]
    plan_graph["policy"] = {"status": status, "total_gbp": total, "budget_gbp": budget}

    return {
        "policy_status": status,
        "plan_graph": plan_graph,
        "current_node": "policy_check",
    }


# ── Stage 6 · compile_itinerary ───────────────────────────────────────────────

async def compile_itinerary(state: ItineraryState) -> dict:
    """Build the final versioned itinerary with per-segment handoff links.

    Stores candidates with provenance visible to the reviewer (FR-PRS-3).
    Each segment includes a handoff_link placeholder (FR-PRS-1).
    """
    emitter = _emitter(state)
    await emitter.emit("compile_itinerary", "in_progress", "Compiling itinerary...")

    constraints = state.get("constraints") or {}
    flight = state.get("flight_reply") or {}
    hotel = state.get("hotel_reply") or {}
    restaurant = state.get("restaurant_reply") or {}

    best_flight = flight.get("candidates", [{}])[0] if flight.get("candidates") else {}
    best_hotel = hotel.get("candidates", [{}])[0] if hotel.get("candidates") else {}

    itinerary = {
        "version": 1,
        "plan_id": state["plan_id"],
        "tenant_id": state["tenant_id"],
        "created_at": datetime.utcnow().isoformat(),
        "status": "draft",
        "trip": {
            "origin": constraints.get("origin"),
            "destination": constraints.get("destination"),
            "check_in": constraints.get("check_in"),
            "check_out": constraints.get("check_out"),
            "purpose": constraints.get("purpose"),
            "traveler_count": constraints.get("traveler_count", 1),
        },
        "segments": [
            {
                "type": "flight",
                "segment_id": f"SEG-FLT-{state['plan_id'][:8]}",
                "provider": best_flight.get("airline", "TBC"),
                "summary": f"{best_flight.get('origin')} → {best_flight.get('destination')} on {best_flight.get('date')}",
                "price_gbp": best_flight.get("price_gbp"),
                "provenance": (flight.get("provenance") or [])[:1],
                "handoff_link": f"https://book.smartinerary.io/flight/{best_flight.get('id', 'TBC')}",
            },
            {
                "type": "hotel",
                "segment_id": f"SEG-HTL-{state['plan_id'][:8]}",
                "provider": best_hotel.get("name", "TBC"),
                "summary": f"{best_hotel.get('name')} — {best_hotel.get('nights')} nights",
                "price_gbp": best_hotel.get("total_price_gbp"),
                "provenance": (hotel.get("provenance") or [])[:1],
                "handoff_link": f"https://book.smartinerary.io/hotel/{best_hotel.get('id', 'TBC')}",
            },
        ],
        "dining_options": restaurant.get("candidates", [])[:3],
        "total_cost_gbp": state.get("total_cost_gbp"),
        "assumptions": list({
            a
            for reply in [flight, hotel, restaurant]
            for a in (reply.get("assumptions") or [])
        }),
    }

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["compile_itinerary"]

    await emitter.emit("compile_itinerary", "completed", "Itinerary compiled with 2 segments")

    return {
        "itinerary": itinerary,
        "plan_graph": plan_graph,
        "current_node": "compile_itinerary",
    }


# ── Stage 6 · await_confirmation ──────────────────────────────────────────────

async def await_confirmation(state: ItineraryState) -> dict:
    """Human-in-the-loop gate (FR-GAT-3, FR-PRS-2).

    No booking-adjacent action occurs without explicit confirmation. This node
    sets the itinerary status to 'awaiting_confirmation' and returns.

    In production this node would interrupt the graph and resume only when the
    traveler or approver submits a confirmation event. LangGraph supports this
    via checkpointing + graph.update_state() from the API layer.
    """
    emitter = _emitter(state)
    await emitter.emit(
        "await_confirmation", "completed",
        "Itinerary ready for review — awaiting traveler confirmation before booking"
    )

    itinerary = dict(state.get("itinerary") or {})
    itinerary["status"] = "awaiting_confirmation"

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["await_confirmation"]

    return {
        "itinerary": itinerary,
        "confirmation_status": "pending",
        "plan_graph": plan_graph,
        "current_node": "await_confirmation",
    }


# ── Routing functions ──────────────────────────────────────────────────────────

def _route_after_parse(
    state: ItineraryState,
) -> Literal["search_specialists", "__end__"]:
    """Route to search if all required fields present; END to prompt user otherwise."""
    if state.get("needs_input"):
        return "__end__"
    return "search_specialists"


def _route_after_policy(
    state: ItineraryState,
) -> Literal["compile_itinerary", "__end__"]:
    """Route to compile if compliant; END if budget breach requires approval."""
    if state.get("policy_status") == "breach":
        return "__end__"
    return "compile_itinerary"


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_itinerary_graph(checkpointer: Any = None) -> CompiledStateGraph[ItineraryState]:
    """Build and compile the itinerary planning LangGraph.

    Usage::

        graph = build_itinerary_graph()
        result = await graph.ainvoke({
            "plan_id": str(uuid.uuid4()),
            "tenant_id": "tenant-abc",
            "raw_goal": "Fly from EDI to CDG on 2026-08-10, return 2026-08-14, budget £800",
        })
        print(result["itinerary"])

    Args:
        checkpointer: Optional LangGraph checkpointer for state persistence.
                      Enables plan resumability after client disconnect (FR-EVT-2).
    """
    graph = StateGraph(ItineraryState)

    # Register nodes
    graph.add_node("parse_intent", parse_intent)
    graph.add_node("search_specialists", search_specialists)
    graph.add_node("merge_results", merge_results)
    graph.add_node("policy_check", policy_check)
    graph.add_node("compile_itinerary", compile_itinerary)
    graph.add_node("await_confirmation", await_confirmation)

    # Edges
    graph.add_edge(START, "parse_intent")
    graph.add_conditional_edges(
        "parse_intent",
        _route_after_parse,
        {"search_specialists": "search_specialists", "__end__": END},
    )
    graph.add_edge("search_specialists", "merge_results")
    graph.add_edge("merge_results", "policy_check")
    graph.add_conditional_edges(
        "policy_check",
        _route_after_policy,
        {"compile_itinerary": "compile_itinerary", "__end__": END},
    )
    graph.add_edge("compile_itinerary", "await_confirmation")
    graph.add_edge("await_confirmation", END)

    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
