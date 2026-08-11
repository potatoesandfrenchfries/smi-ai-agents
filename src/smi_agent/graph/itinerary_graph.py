"""LangGraph itinerary planning workflow for Smartinerary.

Implements the 6-stage end-to-end plan flow defined in the PRD (Section 5):

  START
    │
    ▼
  parse_intent                    Stage 1+2 · Extract Trip with typed constraints (FR-INT-1/2)
    │                             Also classifies trip_type: "business" vs "leisure" from purpose
    ├─ [needs_input] ───────────► END     Missing required fields — caller prompts user (FR-INT-3)
    │
    ├─ [trip_type=business] ──► search_business_specialists   Schedule-priority flights,
    │                            proximity hotels, business-friendly restaurants
    │
    └─ [trip_type=leisure]  ──► search_leisure_specialists    Flights, hotels, restaurants,
                                 + tourist attractions/experiences
                    │
                    ▼           (both paths converge)
  merge_results         Stage 4 · Reconcile candidates, cross-segment feasibility (FR-ORC-4)
    │
    ▼
  policy_check          Stage 4 · Budget + policy compliance gate (FR-SPC-2, FR-ORC-5)
    │
    ├─ [breach] ───────► budget_agent   Suggests cheaper alternative combos, then END
    │                                   (FR-PRS-4 — human approval still required)
    ▼
  compile_itinerary     Stage 6 · Versioned itinerary with per-segment handoff links (FR-PRS-1/3)
    │
    ▼
  reflect_itinerary     Stage 6.5 · Critic pass — validates budget/completeness/quality;
    │                   swaps one already-fetched candidate and loops back to merge_results
    │                   when it finds a fixable issue (max 2 attempts, initial generation only)
    ├─ [needs_regeneration] ──► merge_results (loop)
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

from smi_agent.agents.guardrails import InputGuardrails, sanitize_text
from smi_agent.graph.state import ItineraryState, TaskReply, TaskRequest, TripConstraints
from smi_agent.streaming.step_emitter import NullStepEmitter

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

REQUIRED_FIELDS: list[str] = ["origin", "destination", "check_in", "check_out"]
SUBTASK_DEADLINE_SECONDS = 30
SUBTASK_BUDGET_USD = 0.50

# Cap on in-process re-prompts when an LLM call returns malformed structured
# output — matches Temporal's own activity retry ceiling (see
# activities/itinerary_workflow.py::_default_retry, maximum_attempts=3).
_MAX_LLM_PARSE_RETRIES = 3

# Hotel amenities considered "business-friendly" — used to filter/tag business-path
# hotel candidates and to surface a business_amenities summary in compile_itinerary.
BUSINESS_AMENITIES: set[str] = {"Business centre", "Free WiFi", "Concierge", "Airport shuttle"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _emitter(state: ItineraryState) -> Any:
    return state.get("_step_emitter") or NullStepEmitter()


def _missing_fields(constraints: TripConstraints) -> list[str]:
    return [f for f in REQUIRED_FIELDS if not constraints.get(f)]


def _validate_constraints(constraints: TripConstraints) -> list[str]:
    """Validate constraint *values* once all required fields are present.

    Complements ``_missing_fields`` (presence) with sanity checks on
    dates/budget/destination — catches a garbled or nonsensical goal before
    it reaches specialist search rather than after (FR-INT-2/3).
    """
    issues: list[str] = []

    check_in_dt = check_out_dt = None
    for label, value in (("check_in", constraints.get("check_in")), ("check_out", constraints.get("check_out"))):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError):
            issues.append(f"{label} must be a valid date in YYYY-MM-DD format")
            continue
        if label == "check_in":
            check_in_dt = parsed
        else:
            check_out_dt = parsed

    if check_in_dt is not None and check_in_dt.date() < datetime.utcnow().date():
        issues.append("check_in cannot be in the past")
    if check_in_dt is not None and check_out_dt is not None and check_out_dt <= check_in_dt:
        issues.append("check_out must be after check_in")

    budget_gbp = constraints.get("budget_gbp")
    if budget_gbp is not None and budget_gbp <= 0:
        issues.append("budget_gbp must be a positive number")

    origin = constraints.get("origin")
    destination = constraints.get("destination")
    if origin and destination and origin.strip().lower() == destination.strip().lower():
        issues.append("destination must differ from origin")

    return issues


def _missing_or_invalid(constraints: TripConstraints) -> list[str]:
    """Presence check first — no point reporting invalid values for fields
    that were never extracted in the first place."""
    missing = _missing_fields(constraints)
    return missing if missing else _validate_constraints(constraints)


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


def _classify_trip_type(purpose: str | None) -> Literal["business", "leisure"]:
    """Map the parsed purpose ("business" | "leisure") onto the two graph paths.

    Anything other than an explicit "business" classification — including a
    missing purpose — defaults to the leisure path.
    """
    return "business" if purpose == "business" else "leisure"


def _sort_preference_for(purpose: str) -> str:
    """Derive the flight/hotel sort preference from the business/leisure
    classification instead of guessing it independently from keywords like
    "comfortable" or "cheapest" — business trips default to comfort (schedule
    and business-class priority), leisure trips default to cost (budget-conscious
    sightseeing). This keeps the two JSON fields consistent by construction
    rather than letting the parser infer them separately and risk conflicts.
    """
    return "comfort" if purpose == "business" else "cost"


# ── Stage 1+2 · parse_intent ──────────────────────────────────────────────────

_PARSE_SYSTEM = """You are a travel intent parser. Extract structured fields from the user's travel request.

Return ONLY valid JSON with these fields (use null if not mentioned):
{
  "origin_iata": "3-letter IATA code or city name",
  "destination_iata": "3-letter IATA code or city name",
  "check_in": "YYYY-MM-DD",
  "check_out": "YYYY-MM-DD",
  "budget_gbp": number or null,
  "purpose": "business" | "leisure",
  "traveler_count": number
}

Rules:
- Resolve city names to IATA codes where known (Chennai=MAA, Mumbai=BOM, Delhi=DEL,
  Hyderabad=HYD, Bengaluru=BLR, Lucknow=LKO, Patna=PAT, Kolkata=CCU,
  London=LHR, Edinburgh=EDI, Paris=CDG, Amsterdam=AMS, Dubai=DXB, New York=JFK)
- Infer check_out from duration cues: "4 days from 9th Sept" → check_in=2026-09-09, check_out=2026-09-13
- "purpose" classification: "business" for work trips, meetings, conferences, client
  visits, or corporate travel of any kind; "leisure" for everything else (vacation,
  sightseeing, family visits, honeymoons, general leisure).
- Do NOT try to separately infer cost/comfort/schedule preference from wording like
  "comfortable" or "cheapest" — that is derived downstream from the purpose
  classification above, not parsed independently.
- Convert budget to GBP number (e.g. "600 pounds" → 600, "£500" → 500)
- Today is """ + datetime.utcnow().strftime("%Y-%m-%d") + """
- Return ONLY the JSON object, no explanation."""


async def _llm_parse(goal: str) -> dict | None:
    """Call Gemini Flash Lite (triage lane) to extract structured fields."""
    try:
        from smi_agent.llm.router import LLMRouter
        router = LLMRouter(
            lane="triage",   # → claude-haiku-4-5-20251001 (fast, cheap, perfect for extraction)
            temperature=0.0,
        )
        result = await router.call(
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM},
                {"role": "user", "content": goal},
            ],
            trace_name="parse_intent",
        )
        import json
        text = result.content.strip()
        # Strip markdown code fences if the model wraps output
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text.strip())
        return json.loads(text)
    except Exception as exc:
        logger.warning("[parse_intent] LLM extraction failed (%s) — falling back to regex", exc)
        return None


async def resolve_trip_constraints(raw_goal: str) -> tuple[TripConstraints, list[str]]:
    """Extract typed TripConstraints from a free-text trip goal.

    Primary path: triage-lane LLM extracts all fields including natural date
    expressions ("9th September", "4 days from Monday") and synonyms.
    Fallback: regex keyword extraction when the LLM call fails.

    Pure function — no graph state, no emitter — so it can run both as the
    LangGraph's parse_intent node (below) and standalone, as
    parse_trip_intent_activity does (activities/travel_activities.py) when
    ItineraryWorkflow only received raw_goal with no pre-resolved trip
    fields, before any search activity can be dispatched.

    Returns (constraints, issues) — issues holds missing-required-field
    names, or (once all required fields are present) constraint-validation
    problems like a bad date range or non-positive budget; see
    ``_missing_or_invalid``.
    """
    goal = raw_goal
    goal_lower = goal.lower()

    valid, reason = InputGuardrails.validate(raw_goal)
    if not valid:
        logger.warning("[parse_intent] Rejected by input guardrails: %s", reason)
        empty = TripConstraints(
            origin=None, destination=None, check_in=None, check_out=None,
            budget_gbp=None, purpose="leisure", traveler_count=1, sort_preference="cost",
        )
        return empty, [f"invalid request: {reason}"]

    # ── Primary: LLM extraction ───────────────────────────────────────────────
    parsed = await _llm_parse(goal)

    if parsed:
        origin      = parsed.get("origin_iata") or None
        destination = parsed.get("destination_iata") or None
        check_in    = parsed.get("check_in") or None
        check_out   = parsed.get("check_out") or None
        budget_gbp  = parsed.get("budget_gbp") or None
        purpose     = parsed.get("purpose") or "leisure"
        sort_pref   = _sort_preference_for(purpose)
        traveler_count = int(parsed.get("traveler_count") or 1)

    else:
        # ── Fallback: regex keyword extraction ────────────────────────────────
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", goal)
        check_in  = dates[0] if len(dates) > 0 else None
        check_out = dates[1] if len(dates) > 1 else None

        iata = re.findall(r"\b([A-Z]{3})\b", goal)
        cities = {
            "london": "LHR", "edinburgh": "EDI", "paris": "CDG",
            "amsterdam": "AMS", "berlin": "BER", "madrid": "MAD",
            "rome": "FCO", "new york": "JFK", "dubai": "DXB",
            "chennai": "MAA", "mumbai": "BOM", "delhi": "DEL",
            "hyderabad": "HYD", "bengaluru": "BLR", "bangalore": "BLR",
            "lucknow": "LKO", "patna": "PAT", "kolkata": "CCU",
            "ahmedabad": "AMD", "pune": "PNQ", "goa": "GOI", "kochi": "COK",
        }

        def _cities_in_order(text: str) -> list[str]:
            found = [(text.find(k), v) for k, v in cities.items() if k in text]
            return [v for _, v in sorted(found)]

        ordered = _cities_in_order(goal_lower)
        if len(iata) >= 2:
            origin, destination = iata[0], iata[1]
        elif len(ordered) >= 2:
            origin, destination = ordered[0], ordered[1]
        elif len(ordered) == 1:
            origin, destination = ordered[0], None
        else:
            origin, destination = None, None

        bm = re.search(r"£(\d[\d,]*)", goal) or re.search(r"\b(\d{3,})\s*(?:gbp|pounds?)\b", goal_lower)
        budget_gbp = float(bm.group(1).replace(",", "")) if bm else None
        business_keywords = ["business", "corporate", "meeting", "conference", "client", "work trip"]
        purpose = "business" if any(w in goal_lower for w in business_keywords) else "leisure"
        sort_pref = _sort_preference_for(purpose)
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

    return constraints, _missing_or_invalid(constraints)


async def parse_intent(state: ItineraryState) -> dict:
    """Extract typed TripConstraints from the raw natural-language goal.

    HITL edit re-runs set skip_reparse=True and supply constraints directly
    (e.g. an updated budget_gbp) — trust those as-is instead of re-deriving
    from raw_goal, both because raw_goal hasn't changed (so re-parsing would
    just discard the edit) and because it avoids a wasted LLM call per edit.

    Implements: FR-INT-1 (accept NL goal), FR-INT-2 (parse into Trip + Constraints),
                FR-INT-3 (identify missing fields).
    """
    emitter = _emitter(state)

    if state.get("skip_reparse") and state.get("constraints"):
        constraints = state["constraints"]
        missing = _missing_or_invalid(constraints)
        trip_type = _classify_trip_type(constraints.get("purpose"))
        await emitter.emit("parse_intent", "completed", f"Using edited constraints | trip_type={trip_type}")
        return {
            "constraints": constraints,
            "needs_input": missing,
            "trip_type": trip_type,
            "current_node": "parse_intent",
            "plan_graph": {
                "plan_id": state["plan_id"],
                "raw_goal": state["raw_goal"],
                "parsed_at": datetime.utcnow().isoformat(),
                "stages": ["parse_intent"],
            },
        }

    await emitter.emit("parse_intent", "in_progress", "Parsing travel goal...")

    constraints, missing = await resolve_trip_constraints(state["raw_goal"])
    trip_type = _classify_trip_type(constraints.get("purpose"))

    if missing:
        await emitter.emit(
            "parse_intent", "completed",
            f"Missing required fields: {', '.join(missing)}"
        )
        logger.warning("[parse_intent] Missing fields: %s", missing)
    else:
        await emitter.emit(
            "parse_intent", "completed",
            f"{constraints['origin']} → {constraints['destination']} | "
            f"{constraints['check_in']} to {constraints['check_out']} | trip_type={trip_type}"
        )

    return {
        "constraints": constraints,
        "needs_input": missing,
        "trip_type": trip_type,
        "current_node": "parse_intent",
        "plan_graph": {
            "plan_id": state["plan_id"],
            "raw_goal": state["raw_goal"],
            "parsed_at": datetime.utcnow().isoformat(),
            "stages": ["parse_intent"],
        },
    }


# ── Stage 4 · search_business_specialists / search_leisure_specialists ───────
#
# Both paths are staged as a real agent-to-agent pipeline (FR-ORC-4), not a
# flat fan-out: the flight specialist resolves first and its arrival time is
# handed to the hotel specialist; the resolved hotel is then handed to
# restaurant (and, on the leisure path, attraction) search. Each downstream
# agent genuinely consumes the upstream agent's output — this is not an
# orchestrator diffing results after the fact.

_STANDARD_CHECKIN_HOUR = 14
_LATE_ARRIVAL_HOUR = 22


def checkin_feasibility_note(flight_arrival: str | None) -> str | None:
    """Cross-segment feasibility check: flag when the flight's actual arrival
    time makes standard hotel check-in awkward. Uses the arrival time the
    flight specialist resolved (real agent-to-agent data), so it's None
    whenever no flight has been resolved yet.

    Public so both this graph's run_hotel_search and the Temporal-activity
    path (_generate_itinerary in activities/travel_activities.py) share one
    wording instead of duplicating the logic.
    """
    if not flight_arrival:
        return None
    try:
        arrival_dt = datetime.fromisoformat(flight_arrival)
    except ValueError:
        return None
    if arrival_dt.hour < _STANDARD_CHECKIN_HOUR:
        return (
            f"Flight arrives {arrival_dt.strftime('%H:%M')}, before standard "
            f"{_STANDARD_CHECKIN_HOUR}:00 check-in — early check-in or luggage storage may be needed"
        )
    if arrival_dt.hour >= _LATE_ARRIVAL_HOUR:
        return f"Flight arrives {arrival_dt.strftime('%H:%M')} — confirm 24-hour reception with the hotel"
    return None


def near_hotel_note(hotel_name: str | None) -> str | None:
    """Real location handoff: restaurant/attraction search has no precise
    geocoded radius, but can at least tell the traveler which resolved hotel
    the results should be convenient to.
    """
    if not hotel_name:
        return None
    return f"Selected without a precise radius filter — treat as convenient to {hotel_name}"


def _reused_replies(state: ItineraryState, names: tuple[str, ...]) -> dict[str, TaskReply]:
    """Specialist replies already present in state — e.g. pre-fetched by
    dedicated Temporal search activities before the graph ran (FR-ORC-3: don't
    silently discard and re-fetch work that's already been done). Keyed like
    the dispatch output ("flight_reply", "hotel_reply", ...) so callers can
    merge it straight into their return dict.
    """
    reused = {}
    for name in names:
        existing = state.get(f"{name}_reply")
        if existing and existing.get("candidates"):
            reused[f"{name}_reply"] = existing
    return reused


def _best_field(reply: TaskReply, field: str) -> Any | None:
    """The top candidate's value for `field`, or None with no candidates.

    Used to hand one agent's actual resolved output to the next agent in the
    pipeline (e.g. the flight specialist's arrival time to the hotel
    specialist) — real inter-agent data sharing (FR-ORC-4).
    """
    candidates = reply.get("candidates") or []
    return candidates[0].get(field) if candidates else None


async def _run_one(coro: Any, name: str, errors: list[str]) -> TaskReply:
    """Await a single specialist coroutine outside of asyncio.gather — used
    for the sequential legs of the A2A pipeline — isolating its failure the
    same way _handle_result isolates a gathered one (FR-ORC-5).
    """
    try:
        return await coro
    except Exception as exc:
        logger.error("[specialist_dispatch] %s failed: %s", name, exc)
        errors.append(f"{name}_search: {exc}")
        return TaskReply(status="blocked", candidates=[], assumptions=[], cost_usd=0.0, confidence=0.0, provenance=[])


def _handle_result(result: Any, name: str, errors: list[str]) -> TaskReply:
    """Convert asyncio.gather result (value or exception) to a TaskReply."""
    if isinstance(result, BaseException):
        logger.error("[specialist_dispatch] %s failed: %s", name, result)
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


async def search_business_specialists(state: ItineraryState) -> dict:
    """Business path (FR-ORC-1): flights ranked by schedule, hotels ranked by
    proximity to the business location, restaurants filtered to business-friendly
    options. No attraction specialist is invoked — sightseeing is out of scope
    for a business trip (only the required agents run, per-path).

    Fully sequential (flight -> hotel -> restaurant): with only three agents
    and each one genuinely consuming the previous agent's output, there's
    nothing left to run concurrently.
    """
    emitter = _emitter(state)
    constraints = state["constraints"]
    plan_id = state["plan_id"]
    errors: list[str] = list(state.get("errors") or [])

    reused = _reused_replies(state, ("flight", "hotel", "restaurant"))
    task_requests: dict[str, TaskRequest] = {}

    # ── Stage 1: flight (feeds hotel below) ──────────────────────────────────
    if "flight_reply" in reused:
        flight_reply = reused["flight_reply"]
    else:
        flight_task = _make_task_request(
            goal=f"Find schedule-priority flights from {constraints['origin']} to {constraints['destination']} on {constraints['check_in']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["flight"] = flight_task
        await emitter.emit("search_business_specialists", "in_progress", "Searching schedule-priority flights...")
        flight_reply = await _run_one(
            run_flight_search(flight_task, constraints, sort_override="time"), "flight", errors,
        )

    flight_arrival = _best_field(flight_reply, "arrival")

    # ── Stage 2: hotel, receives the flight's arrival time (feeds restaurant below) ──
    if "hotel_reply" in reused:
        hotel_reply = reused["hotel_reply"]
    else:
        hotel_task = _make_task_request(
            goal=f"Find hotels near the business location in {constraints['destination']} from {constraints['check_in']} to {constraints['check_out']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["hotel"] = hotel_task
        await emitter.emit("search_business_specialists", "in_progress", "Searching proximity hotels...")
        hotel_reply = await _run_one(
            run_hotel_search(
                hotel_task, constraints, sort_override="proximity", business_only=True,
                flight_arrival=flight_arrival,
            ),
            "hotel", errors,
        )

    hotel_name = _best_field(hotel_reply, "name")

    # ── Stage 3: restaurant, receives the resolved hotel location ────────────
    if "restaurant_reply" in reused:
        restaurant_reply = reused["restaurant_reply"]
    else:
        restaurant_task = _make_task_request(
            goal=f"Find business-friendly restaurants near {constraints['destination']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["restaurant"] = restaurant_task
        await emitter.emit("search_business_specialists", "in_progress", "Searching business-friendly restaurants...")
        restaurant_reply = await _run_one(
            run_restaurant_search(restaurant_task, constraints, business_only=True, near_hotel=hotel_name),
            "restaurant", errors,
        )

    replies: dict[str, TaskReply] = {
        "flight_reply": flight_reply, "hotel_reply": hotel_reply, "restaurant_reply": restaurant_reply,
    }

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["dispatched_at"] = plan_graph.get("dispatched_at", datetime.utcnow().isoformat())
    plan_graph["tasks"] = {name: req["task_id"] for name, req in task_requests.items()}
    plan_graph["stages"] = plan_graph.get("stages", []) + ["search_business_specialists"]
    plan_graph["completed_at"] = datetime.utcnow().isoformat()

    await emitter.emit(
        "search_business_specialists", "completed",
        ", ".join(f"{name.removesuffix('_reply').title()}: {len(reply['candidates'])}" for name, reply in replies.items())
    )

    return {**replies, "plan_graph": plan_graph, "errors": errors, "current_node": "search_business_specialists"}


async def search_leisure_specialists(state: ItineraryState) -> dict:
    """Leisure path (FR-ORC-1): flights/hotels ranked by the traveler's stated
    style and budget (sort_preference), plus restaurants and — unique to this
    path — a tourist attraction/experience specialist for sightseeing.

    Staged flight -> hotel -> {restaurant, attraction}: restaurant and
    attraction run concurrently with each other (neither depends on the
    other), but both wait on the resolved hotel location.
    """
    emitter = _emitter(state)
    constraints = state["constraints"]
    plan_id = state["plan_id"]
    errors: list[str] = list(state.get("errors") or [])

    reused = _reused_replies(state, ("flight", "hotel", "restaurant", "attraction"))
    task_requests: dict[str, TaskRequest] = {}

    # ── Stage 1: flight (feeds hotel below) ──────────────────────────────────
    if "flight_reply" in reused:
        flight_reply = reused["flight_reply"]
    else:
        flight_task = _make_task_request(
            goal=f"Find flights from {constraints['origin']} to {constraints['destination']} on {constraints['check_in']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["flight"] = flight_task
        await emitter.emit("search_leisure_specialists", "in_progress", "Searching flights...")
        flight_reply = await _run_one(run_flight_search(flight_task, constraints), "flight", errors)

    flight_arrival = _best_field(flight_reply, "arrival")

    # ── Stage 2: hotel, receives the flight's arrival time (feeds restaurant/attraction below) ──
    if "hotel_reply" in reused:
        hotel_reply = reused["hotel_reply"]
    else:
        hotel_task = _make_task_request(
            goal=f"Find hotels in {constraints['destination']} from {constraints['check_in']} to {constraints['check_out']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["hotel"] = hotel_task
        await emitter.emit("search_leisure_specialists", "in_progress", "Searching hotels...")
        hotel_reply = await _run_one(
            run_hotel_search(hotel_task, constraints, flight_arrival=flight_arrival), "hotel", errors,
        )

    hotel_name = _best_field(hotel_reply, "name")

    # ── Stage 3: restaurant + attraction, receive the resolved hotel location ──
    coros: dict[str, Any] = {}
    if "restaurant_reply" not in reused:
        restaurant_task = _make_task_request(
            goal=f"Find restaurants in {constraints['destination']}"
                 + (f", convenient to {hotel_name}" if hotel_name else ""),
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["restaurant"] = restaurant_task
        coros["restaurant"] = run_restaurant_search(restaurant_task, constraints, near_hotel=hotel_name)

    if "attraction_reply" not in reused:
        attraction_task = _make_task_request(
            goal=f"Find tourist attractions and experiences in {constraints['destination']}",
            constraints=constraints, plan_id=plan_id, budget_usd=SUBTASK_BUDGET_USD,
        )
        task_requests["attraction"] = attraction_task
        coros["attraction"] = run_attraction_search(attraction_task, constraints, near_hotel=hotel_name)

    replies: dict[str, TaskReply] = dict(reused)
    replies["flight_reply"] = flight_reply
    replies["hotel_reply"] = hotel_reply

    if coros:
        await emitter.emit(
            "search_leisure_specialists", "in_progress",
            f"Searching restaurants and attractions near {hotel_name}..." if hotel_name
            else "Searching restaurants and attractions...",
        )
        names = list(coros.keys())
        results = await asyncio.gather(*coros.values(), return_exceptions=True)
        for name, result in zip(names, results, strict=True):
            replies[f"{name}_reply"] = _handle_result(result, name, errors)

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["dispatched_at"] = plan_graph.get("dispatched_at", datetime.utcnow().isoformat())
    plan_graph["tasks"] = {name: req["task_id"] for name, req in task_requests.items()}
    plan_graph["stages"] = plan_graph.get("stages", []) + ["search_leisure_specialists"]
    plan_graph["completed_at"] = datetime.utcnow().isoformat()

    await emitter.emit(
        "search_leisure_specialists", "completed",
        ", ".join(f"{name.removesuffix('_reply').title()}: {len(reply['candidates'])}" for name, reply in replies.items())
    )

    return {**replies, "plan_graph": plan_graph, "errors": errors, "current_node": "search_leisure_specialists"}


async def run_flight_search(
    task: TaskRequest, constraints: TripConstraints, sort_override: str | None = None,
) -> TaskReply:
    from smi_agent.examples.travel.tools.location_resolver import to_iata
    from smi_agent.providers.explain import annotate_reasons
    from smi_agent.providers.registry import get_flight_provider
    sort_by = sort_override or constraints.get("sort_preference", "cost")
    results = await get_flight_provider().search(
        origin=to_iata(constraints["origin"] or ""),
        destination=to_iata(constraints["destination"] or ""),
        date=constraints["check_in"] or "",
        sort_by=sort_by,
    )
    results = annotate_reasons(results, sort_by=sort_by, price_field="price_gbp")
    assumptions = ["Economy class assumed if not specified"]
    if sort_override == "time":
        assumptions.append("Ranked by shortest journey time as a schedule-priority proxy — no meeting time was supplied")
    return _candidates_to_reply(results, assumptions=assumptions)


async def run_hotel_search(
    task: TaskRequest,
    constraints: TripConstraints,
    sort_override: str | None = None,
    business_only: bool = False,
    flight_arrival: str | None = None,
) -> TaskReply:
    from smi_agent.examples.travel.tools.location_resolver import to_city_name
    from smi_agent.providers.explain import annotate_reasons
    from smi_agent.providers.registry import get_hotel_provider
    sort_by = sort_override or ("rating" if constraints.get("sort_preference") == "comfort" else "price")
    results = await get_hotel_provider().search(
        location=to_city_name(constraints["destination"] or ""),
        check_in=constraints["check_in"] or "",
        check_out=constraints["check_out"] or "",
        sort_by=sort_by,
    )
    assumptions = ["Double room assumed if not specified"]
    if business_only:
        filtered = [h for h in results if BUSINESS_AMENITIES & set(h.get("amenities", []))]
        if filtered:
            results = filtered
            assumptions.append("Filtered to hotels offering business amenities (business centre / wifi / concierge)")
        else:
            assumptions.append("No hotels matched business amenities — falling back to proximity ranking")
    results = annotate_reasons(
        results, sort_by=sort_by, price_field="total_price_gbp",
        rating_field="rating", proximity_field="distance_from_centre_km",
    )
    feasibility_note = checkin_feasibility_note(flight_arrival)
    if feasibility_note:
        assumptions.append(feasibility_note)
    return _candidates_to_reply(results, assumptions=assumptions)


async def run_restaurant_search(
    task: TaskRequest, constraints: TripConstraints, business_only: bool = False, near_hotel: str | None = None,
) -> TaskReply:
    from smi_agent.examples.travel.tools.location_resolver import to_city_name
    from smi_agent.providers.explain import annotate_reasons
    from smi_agent.providers.registry import get_restaurant_provider
    results = await get_restaurant_provider().search(location=to_city_name(constraints["destination"] or ""))
    assumptions = ["Dinner assumed if meal type not specified"]
    if business_only:
        filtered = [r for r in results if r.get("business_friendly")]
        if filtered:
            results = filtered
            assumptions.append("Filtered to business-friendly restaurants suitable for client dining")
        else:
            assumptions.append("No restaurants flagged business-friendly — falling back to top-rated options")
    results = annotate_reasons(results, sort_by="rating", price_field="avg_spend_per_person_gbp", rating_field="rating")
    note = near_hotel_note(near_hotel)
    if note:
        assumptions.append(note)
    return _candidates_to_reply(results, assumptions=assumptions)


async def run_attraction_search(
    task: TaskRequest, constraints: TripConstraints, near_hotel: str | None = None,
) -> TaskReply:
    from smi_agent.examples.travel.tools.attraction_scraper import search_attractions
    from smi_agent.examples.travel.tools.location_resolver import to_city_name
    from smi_agent.providers.explain import annotate_reasons
    sort_by = "price" if constraints.get("sort_preference") == "cost" else "rating"
    results = await search_attractions(location=to_city_name(constraints["destination"] or ""), sort_by=sort_by)
    results = annotate_reasons(results, sort_by=sort_by, price_field="entry_fee_gbp", rating_field="rating")
    assumptions = ["Half-day sightseeing pace assumed unless specified"]
    note = near_hotel_note(near_hotel)
    if note:
        assumptions.append(note)
    return _candidates_to_reply(results, assumptions=assumptions)


# ── Stage 4 · merge_results ───────────────────────────────────────────────────

def _decision_log_entries(state: ItineraryState) -> list[dict]:
    """Decision logging (Explainable Recommendations + Decision Logging):
    for each section, record which candidate was selected and which were
    rejected, with the `reason` each one already carries from
    ``providers.explain.annotate_reasons`` — the top candidate's reason
    explains the pick, every other candidate's reason (generated relative to
    the top pick) doubles as the rejection explanation.

    Recomputed fresh every time merge_results runs (including reflect_itinerary
    loop-backs after a candidate swap), so it always reflects the current
    state rather than accumulating stale entries across attempts.
    """
    entries: list[dict] = []
    for section in ("flight", "hotel", "restaurant", "attraction"):
        reply = state.get(f"{section}_reply") or {}
        candidates = reply.get("candidates") or []
        if not candidates:
            continue
        selected = candidates[0]
        entries.append({
            "section": section,
            "selected_id": selected.get("id"),
            "selected_reason": selected.get("reason", ""),
            "rejected": [
                {"id": c.get("id"), "reason": c.get("reason", "")}
                for c in candidates[1:]
            ],
        })
    return entries


async def merge_results(state: ItineraryState) -> dict:
    """Reconcile specialist candidates into a coherent plan.

    Cross-segment feasibility (e.g. hotel check-in vs. flight arrival) is
    checked earlier, in run_hotel_search, using the arrival time handed down
    from the flight specialist (FR-ORC-4) — by the time results reach here
    they're already reconciled, so this only picks the best candidate per
    segment and totals the cost.
    """
    emitter = _emitter(state)
    await emitter.emit("merge_results", "in_progress", "Reconciling candidates...")

    flight = state.get("flight_reply") or {}
    hotel = state.get("hotel_reply") or {}
    restaurant = state.get("restaurant_reply") or {}
    attraction = state.get("attraction_reply") or {}

    # Best candidate per segment — candidates are already provider-ranked and
    # explain-annotated (providers.explain.annotate_reasons), so [0] is both
    # "the pick" and self-explaining via its own `reason` field.
    best_flight = flight.get("candidates", [{}])[0] if flight.get("candidates") else {}
    best_hotel = hotel.get("candidates", [{}])[0] if hotel.get("candidates") else {}
    best_restaurants = restaurant.get("candidates", [])[:3]
    best_attractions = attraction.get("candidates", [])[:3]  # empty on the business path

    # Compute total estimated cost
    total_cost = (
        (best_flight.get("price_gbp") or 0) +
        (best_hotel.get("total_price_gbp") or 0) +
        sum(r.get("avg_spend_per_person_gbp") or 0 for r in best_restaurants) +
        sum(a.get("entry_fee_gbp") or 0 for a in best_attractions)
    )

    decision_log = _decision_log_entries(state)

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["merge_results"]
    plan_graph["merge"] = {
        "best_flight_id": best_flight.get("id"),
        "best_hotel_id": best_hotel.get("id"),
        "restaurant_count": len(best_restaurants),
        "attraction_count": len(best_attractions),
        "total_cost_gbp": round(total_cost, 2),
    }

    await emitter.emit("merge_results", "completed", f"Total estimated cost: £{total_cost:.2f}")

    return {
        "plan_graph": plan_graph,
        "total_cost_gbp": round(total_cost, 2),
        "decision_log": decision_log,
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


# ── Stage 5 · budget_agent ────────────────────────────────────────────────────

def _combo_cost(
    flight: dict, hotel: dict, restaurants: list[dict], attractions: list[dict],
) -> float:
    return round(
        (flight.get("price_gbp") or 0)
        + (hotel.get("total_price_gbp") or 0)
        + sum(r.get("avg_spend_per_person_gbp") or 0 for r in restaurants)
        + sum(a.get("entry_fee_gbp") or 0 for a in attractions),
        2,
    )


async def budget_agent(state: ItineraryState) -> dict:
    """Suggest cheaper alternative combinations when the plan breaches budget.

    Re-ranks the specialist candidates already gathered in this plan — no new
    searches are issued, the numbers needed to answer "what's cheaper" are
    already in state (FR-ORC-3: no full re-fetch just to compare cost). Each
    alternative swaps out one or more segments for its cheapest available
    option and reports the resulting total and savings, so the traveler picks
    a concrete trade-off instead of a generic "over budget" message.
    """
    emitter = _emitter(state)
    await emitter.emit("budget_agent", "in_progress", "Looking for cheaper alternatives...")

    budget = (state.get("constraints") or {}).get("budget_gbp")
    trip_type = state.get("trip_type", "leisure")
    current_total = state.get("total_cost_gbp") or 0.0

    flights = (state.get("flight_reply") or {}).get("candidates", [])
    hotels = (state.get("hotel_reply") or {}).get("candidates", [])
    restaurants = (state.get("restaurant_reply") or {}).get("candidates", [])
    attractions = (state.get("attraction_reply") or {}).get("candidates", [])

    current_flight = flights[0] if flights else {}
    current_hotel = hotels[0] if hotels else {}
    current_restaurants = restaurants[:3]
    current_attractions = attractions[:3] if trip_type == "leisure" else []

    cheapest_flight = min(flights, key=lambda f: f.get("price_gbp") or float("inf"), default={})
    cheapest_hotel = min(hotels, key=lambda h: h.get("total_price_gbp") or float("inf"), default={})
    cheapest_restaurants = sorted(
        restaurants, key=lambda r: r.get("avg_spend_per_person_gbp") or float("inf")
    )[:3]

    candidates: list[dict] = []

    if cheapest_hotel and cheapest_hotel.get("id") != current_hotel.get("id"):
        total = _combo_cost(current_flight, cheapest_hotel, current_restaurants, current_attractions)
        candidates.append({
            "label": f"Switch hotel to {cheapest_hotel.get('name', 'a cheaper option')}",
            "total_cost_gbp": total,
        })

    if cheapest_flight and cheapest_flight.get("id") != current_flight.get("id"):
        total = _combo_cost(cheapest_flight, current_hotel, current_restaurants, current_attractions)
        candidates.append({
            "label": f"Switch flight to {cheapest_flight.get('airline', 'a cheaper option')} "
                     f"({cheapest_flight.get('departure', 'alternate time')})",
            "total_cost_gbp": total,
        })

    combo_label = "Switch to the cheapest flight, hotel, and dining combo"
    if trip_type == "leisure" and attractions:
        combo_label += " and skip the optional attractions"
    candidates.append({
        "label": combo_label,
        "total_cost_gbp": _combo_cost(cheapest_flight, cheapest_hotel, cheapest_restaurants, []),
    })

    # De-dupe identical totals (e.g. only one hotel/flight tier available) and
    # only surface combos that actually save money, cheapest first, capped at 3.
    seen_totals: set[float] = set()
    alternatives: list[dict] = []
    for c in sorted(candidates, key=lambda c: c["total_cost_gbp"]):
        savings = round(current_total - c["total_cost_gbp"], 2)
        if savings <= 0 or c["total_cost_gbp"] in seen_totals:
            continue
        seen_totals.add(c["total_cost_gbp"])
        alternatives.append({
            "label": c["label"],
            "total_cost_gbp": c["total_cost_gbp"],
            "savings_gbp": savings,
            "within_budget": bool(budget) and c["total_cost_gbp"] <= budget,
        })
        if len(alternatives) == 3:
            break

    await emitter.emit(
        "budget_agent", "completed",
        f"Found {len(alternatives)} cheaper alternative(s)" if alternatives
        else "No cheaper alternatives found among current candidates — consider raising the budget"
    )

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["budget_agent"]

    # Record the breach decision itself: current combo rejected for exceeding
    # budget, alternatives offered in its place — same decision-log shape as
    # merge_results' per-section entries, keyed "budget" instead of a section.
    decision_log = list(state.get("decision_log") or [])
    decision_log.append({
        "section": "budget",
        "selected_id": None,
        "selected_reason": f"Current combo (£{current_total:.2f}) exceeds budget (£{budget:.2f}) — held for approval" if budget else "Budget breach detected",
        "rejected": [
            {"id": None, "reason": f"{a['label']} (saves £{a['savings_gbp']:.2f})"}
            for a in alternatives
        ],
    })

    return {
        "budget_alternatives": alternatives,
        "plan_graph": plan_graph,
        "decision_log": decision_log,
        "current_node": "budget_agent",
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
    trip_type = state.get("trip_type", "leisure")
    flight = state.get("flight_reply") or {}
    hotel = state.get("hotel_reply") or {}
    restaurant = state.get("restaurant_reply") or {}
    attraction = state.get("attraction_reply") or {}

    best_flight = flight.get("candidates", [{}])[0] if flight.get("candidates") else {}
    best_hotel = hotel.get("candidates", [{}])[0] if hotel.get("candidates") else {}
    best_attractions = attraction.get("candidates", [])[:3]

    def _reason(text: str | None) -> str | None:
        # Specialist-written free text — sanitize before it reaches the
        # traveler, same as any other LLM-authored output (FR-PRS-3).
        return sanitize_text(text) if text else text

    segments = [
        {
            "type": "flight",
            "segment_id": f"SEG-FLT-{state['plan_id'][:8]}",
            "provider": best_flight.get("airline", "TBC"),
            "summary": f"{best_flight.get('origin')} → {best_flight.get('destination')} on {best_flight.get('date')}",
            "price_gbp": best_flight.get("price_gbp"),
            "reason": _reason(best_flight.get("reason")),
            "provenance": (flight.get("provenance") or [])[:1],
            "handoff_link": f"https://book.smartinerary.io/flight/{best_flight.get('id', 'TBC')}",
        },
        {
            "type": "hotel",
            "segment_id": f"SEG-HTL-{state['plan_id'][:8]}",
            "provider": best_hotel.get("name", "TBC"),
            "summary": f"{best_hotel.get('name')} — {best_hotel.get('nights')} nights",
            "price_gbp": best_hotel.get("total_price_gbp"),
            "reason": _reason(best_hotel.get("reason")),
            "provenance": (hotel.get("provenance") or [])[:1],
            "handoff_link": f"https://book.smartinerary.io/hotel/{best_hotel.get('id', 'TBC')}",
        },
    ]

    if trip_type == "leisure" and best_attractions:
        segments.append({
            "type": "attraction",
            "segment_id": f"SEG-ATT-{state['plan_id'][:8]}",
            "provider": "Sightseeing",
            "summary": ", ".join(a.get("name", "TBC") for a in best_attractions),
            "price_gbp": round(sum(a.get("entry_fee_gbp") or 0 for a in best_attractions), 2),
            "reason": _reason(best_attractions[0].get("reason")) if best_attractions else None,
            "provenance": (attraction.get("provenance") or [])[:3],
            "handoff_link": None,
        })

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
            "trip_type": trip_type,
            "traveler_count": constraints.get("traveler_count", 1),
            "optimized_for": "meetings and work commitments" if trip_type == "business" else "sightseeing and relaxation",
        },
        "segments": segments,
        "dining_options": restaurant.get("candidates", [])[:3],
        # Leisure-only: recommended attractions plus a deliberately unscheduled
        # block so the itinerary isn't packed wall-to-wall (FR-PRS leisure spec).
        "attractions": best_attractions,
        "free_time_note": (
            "Afternoons intentionally left open for self-guided exploration or relaxation"
            if trip_type == "leisure" else None
        ),
        # Business-only: amenities pulled from the chosen hotel, surfaced separately
        # so a traveler can see at a glance what supports their work commitments.
        "business_amenities": (
            sorted(BUSINESS_AMENITIES & set(best_hotel.get("amenities", [])))
            if trip_type == "business" else []
        ),
        "total_cost_gbp": state.get("total_cost_gbp"),
        "assumptions": list({
            a
            for reply in [flight, hotel, restaurant, attraction]
            for a in (reply.get("assumptions") or [])
        }),
        # Decision Logging: selected vs. rejected candidates per section, with
        # the reason each carries (providers.explain.annotate_reasons) —
        # populated by merge_results, extended by budget_agent on a breach.
        "decision_log": state.get("decision_log") or [],
    }

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["compile_itinerary"]

    await emitter.emit(
        "compile_itinerary", "completed",
        f"Itinerary compiled with {len(segments)} segments ({trip_type})"
    )

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


# ── Stage 6.5 · reflect_itinerary ─────────────────────────────────────────────

_MAX_REFLECTION_ATTEMPTS = 2
_REFLECTION_PROMPT_DIR = "prompts/agents/reflection"


def _completeness_issues(itinerary: dict, trip_type: str) -> list[dict]:
    issues: list[dict] = []
    segments_by_type = {s.get("type"): s for s in itinerary.get("segments", [])}

    flight = segments_by_type.get("flight")
    if not flight or not flight.get("provider") or flight.get("provider") == "TBC":
        issues.append({"section": "flight", "problem": "No confirmed flight option available"})

    hotel = segments_by_type.get("hotel")
    if not hotel or not hotel.get("provider") or hotel.get("provider") == "TBC":
        issues.append({"section": "hotel", "problem": "No confirmed hotel option available"})

    if not itinerary.get("dining_options"):
        issues.append({"section": "restaurant", "problem": "No dining options found"})

    if trip_type == "leisure" and not itinerary.get("attractions"):
        issues.append({"section": "attraction", "problem": "No attractions found for a leisure trip"})

    return issues


def _budget_status(itinerary: dict, constraints: TripConstraints) -> dict:
    """Report budget compliance for the traveler-facing quality_review.

    Not a new check: policy_check already routes any breach to budget_agent
    before compile_itinerary (and therefore reflect_itinerary) ever runs, so
    this can never find a breach — it just surfaces the already-validated
    figures for visibility alongside the completeness/quality findings.
    """
    budget = constraints.get("budget_gbp")
    total = itinerary.get("total_cost_gbp") or 0.0
    return {
        "total_gbp": total,
        "budget_gbp": budget,
        "within_budget": not budget or total <= budget,
    }


class ReflectionAgent:
    """Critic that reviews a compiled itinerary for completeness and quality.

    Structured the same way as the specialist agents in agents/specialists/*.py
    (name/description/run()), but — like IntentClassifierAgent — deliberately
    independent of BaseSpecialist/StructuredResponse, since this belongs to
    the active Temporal + LangGraph pipeline and returns a plain dict that
    reflect_itinerary folds into ItineraryState.
    """

    @property
    def name(self) -> str:
        return "reflection"

    @property
    def description(self) -> str:
        return "Reviews a compiled itinerary for completeness and quality issues (budget is reported, not re-checked — see _budget_status)."

    async def run(self, itinerary: dict, constraints: TripConstraints, trip_type: str) -> dict:
        """Returns {"quality_ok", "issues", "notes", "budget"}."""
        issues = _completeness_issues(itinerary, trip_type)

        llm_review = await self._llm_quality_review(itinerary)
        issues.extend(llm_review.get("issues", []))

        return {
            "quality_ok": not issues and llm_review.get("quality_ok", True),
            "issues": issues,
            "notes": llm_review.get("notes", ""),
            "budget": _budget_status(itinerary, constraints),
        }

    async def _llm_quality_review(self, itinerary: dict) -> dict:
        try:
            import json

            from smi_agent.llm.prompts import PromptLoader
            from smi_agent.llm.router import LLMRouter

            prompt_loader = PromptLoader(_REFLECTION_PROMPT_DIR)
            system_block = prompt_loader.render_system("system", {})

            summary = {
                "trip": itinerary.get("trip"),
                "segments": [
                    {"type": s.get("type"), "summary": s.get("summary"), "price_gbp": s.get("price_gbp")}
                    for s in itinerary.get("segments", [])
                ],
                "total_cost_gbp": itinerary.get("total_cost_gbp"),
                "assumptions": itinerary.get("assumptions"),
            }
            router = LLMRouter(lane="middle", temperature=0.1)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": [system_block]},
                {"role": "user", "content": json.dumps(summary)},
            ]

            # Malformed JSON gets re-prompted in-process rather than raised —
            # an exception here would make the whole Temporal activity
            # succeed-or-fail on this one sub-check, discarding a compiled
            # itinerary over a parse hiccup. Capped at _MAX_LLM_PARSE_RETRIES
            # (3), matching this codebase's Temporal activity retry convention.
            last_exc: Exception | None = None
            for attempt in range(_MAX_LLM_PARSE_RETRIES):
                result = await router.call(messages=messages, trace_name="reflect_itinerary")
                text = result.content.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```[a-z]*\n?", "", text)
                    text = re.sub(r"\n?```$", "", text.strip())
                try:
                    parsed = json.loads(text)
                    return {
                        "quality_ok": bool(parsed.get("quality_ok", True)),
                        "issues": parsed.get("issues", []),
                        "notes": sanitize_text(parsed.get("notes", "")),
                    }
                except (json.JSONDecodeError, TypeError) as exc:
                    last_exc = exc
                    logger.warning(
                        "[%s] quality review JSON parse failed (attempt %d/%d): %s",
                        self.name, attempt + 1, _MAX_LLM_PARSE_RETRIES, exc,
                    )
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({
                        "role": "user",
                        "content": "That was not valid JSON. Return ONLY the JSON object — no markdown fences, no explanation.",
                    })
            raise last_exc or RuntimeError("quality review: no attempts made")
        except Exception as exc:
            # Fail closed, not open: a review we couldn't run is not the same
            # as a review that passed. Surfaced to the traveler via
            # quality_ok=False rather than silently waved through.
            logger.warning(
                "[%s] LLM quality review failed after %d attempt(s) (%s) — failing closed",
                self.name, _MAX_LLM_PARSE_RETRIES, exc,
            )
            return {
                "quality_ok": False,
                "issues": [{"section": None, "problem": "Automated quality review could not be completed"}],
                "notes": "Quality review unavailable — please double-check this itinerary manually",
            }


def _promote_next_untried(candidates: list[dict], tried_ids: list[str]) -> dict | None:
    """Pick the best already-fetched candidate not yet tried by reflection.

    Mirrors _reorder_to_front in activities/itinerary_workflow.py (promote a
    candidate that's already been searched for, never trigger a new search)
    but stays local to this module — graph/ doesn't import from activities/.
    """
    for candidate in candidates:
        if candidate.get("id") not in tried_ids:
            return candidate
    return None


async def reflect_itinerary(state: ItineraryState) -> dict:
    """Critic pass: validate budget, completeness, and quality before the
    traveler ever sees the itinerary. When a specific section is at fault and
    an untried already-fetched candidate exists, swap to it and loop back
    through merge/policy/compile — no new search is issued (FR-ORC-3).

    Only auto-swaps on the initial generation, never on a HITL edit re-run
    (skip_reparse=True): an edit is the traveler's deliberate choice and
    should be flagged if it looks off, not silently overridden.
    """
    emitter = _emitter(state)
    await emitter.emit("reflect_itinerary", "in_progress", "Reviewing itinerary...")

    itinerary = state.get("itinerary") or {}
    constraints = state.get("constraints") or {}
    trip_type = state.get("trip_type", "leisure")
    attempts = state.get("reflection_attempts", 0)
    tried = {k: list(v) for k, v in (state.get("tried_candidate_ids") or {}).items()}

    quality_review = await ReflectionAgent().run(itinerary, constraints, trip_type)
    issues = quality_review["issues"]

    plan_graph = dict(state.get("plan_graph") or {})
    plan_graph["stages"] = plan_graph.get("stages", []) + ["reflect_itinerary"]

    can_retry = not state.get("skip_reparse") and attempts < _MAX_REFLECTION_ATTEMPTS
    fixable_sections = {"flight", "hotel", "restaurant", "attraction"}
    replies_by_section = {
        "flight": state.get("flight_reply") or {},
        "hotel": state.get("hotel_reply") or {},
        "restaurant": state.get("restaurant_reply") or {},
        "attraction": state.get("attraction_reply") or {},
    }

    if can_retry:
        for issue in issues:
            section = issue.get("section")
            if section not in fixable_sections:
                continue
            candidates = replies_by_section[section].get("candidates", [])
            section_tried = tried.setdefault(section, [])
            next_candidate = _promote_next_untried(candidates, section_tried)
            if next_candidate is None:
                continue

            section_tried.append(next_candidate.get("id"))
            reordered = sorted(candidates, key=lambda c: c.get("id") != next_candidate.get("id"))
            new_reply = dict(replies_by_section[section])
            new_reply["candidates"] = reordered

            await emitter.emit(
                "reflect_itinerary", "in_progress",
                f"Swapping {section} to address: {issue.get('problem')}",
            )

            itinerary_with_review = dict(itinerary)
            itinerary_with_review["quality_review"] = quality_review

            return {
                f"{section}_reply": new_reply,
                "tried_candidate_ids": tried,
                "reflection_attempts": attempts + 1,
                "quality_review": quality_review,
                "itinerary": itinerary_with_review,
                "plan_graph": plan_graph,
                "current_node": "reflect_itinerary",
                "_needs_regeneration": True,
            }

    itinerary_with_review = dict(itinerary)
    itinerary_with_review["quality_review"] = quality_review

    await emitter.emit(
        "reflect_itinerary", "completed",
        "No issues found" if quality_review["quality_ok"] else f"{len(issues)} issue(s) noted for the traveler",
    )

    return {
        "itinerary": itinerary_with_review,
        "quality_review": quality_review,
        "reflection_attempts": attempts,
        "tried_candidate_ids": tried,
        "plan_graph": plan_graph,
        "current_node": "reflect_itinerary",
        "_needs_regeneration": False,
    }


# ── Routing functions ──────────────────────────────────────────────────────────

def _route_after_parse(
    state: ItineraryState,
) -> Literal["search_business_specialists", "search_leisure_specialists", "__end__"]:
    """Route to the business or leisure specialist path, or END to prompt the user.

    Missing required fields (FR-INT-3) take priority over purpose-based routing —
    there's no point dispatching specialists for a trip we can't yet search.
    Otherwise, the trip_type computed in parse_intent (from constraints.purpose)
    decides which of the two paths runs — only the required agents are invoked
    for each (FR-ORC-1): the leisure path adds the attraction specialist, the
    business path does not.
    """
    if state.get("needs_input"):
        return "__end__"
    return (
        "search_business_specialists"
        if state.get("trip_type") == "business"
        else "search_leisure_specialists"
    )


def _route_after_policy(
    state: ItineraryState,
) -> Literal["compile_itinerary", "budget_agent"]:
    """Route to compile if compliant; to the Budget Agent if breach requires approval."""
    if state.get("policy_status") == "breach":
        return "budget_agent"
    return "compile_itinerary"


def _route_after_reflect(
    state: ItineraryState,
) -> Literal["merge_results", "await_confirmation"]:
    """Loop back to recompute cost/policy/compile after a section swap, or
    proceed to the HITL gate once the critic has nothing left to fix.
    """
    if state.get("_needs_regeneration"):
        return "merge_results"
    return "await_confirmation"


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
    graph.add_node("search_business_specialists", search_business_specialists)
    graph.add_node("search_leisure_specialists", search_leisure_specialists)
    graph.add_node("merge_results", merge_results)
    graph.add_node("policy_check", policy_check)
    graph.add_node("budget_agent", budget_agent)
    graph.add_node("compile_itinerary", compile_itinerary)
    graph.add_node("reflect_itinerary", reflect_itinerary)
    graph.add_node("await_confirmation", await_confirmation)

    # Edges
    graph.add_edge(START, "parse_intent")
    graph.add_conditional_edges(
        "parse_intent",
        _route_after_parse,
        {
            "search_business_specialists": "search_business_specialists",
            "search_leisure_specialists": "search_leisure_specialists",
            "__end__": END,
        },
    )
    # Both paths converge on the same downstream merge/policy/compile flow.
    graph.add_edge("search_business_specialists", "merge_results")
    graph.add_edge("search_leisure_specialists", "merge_results")
    graph.add_edge("merge_results", "policy_check")
    graph.add_conditional_edges(
        "policy_check",
        _route_after_policy,
        {"compile_itinerary": "compile_itinerary", "budget_agent": "budget_agent"},
    )
    graph.add_edge("compile_itinerary", "reflect_itinerary")
    graph.add_conditional_edges(
        "reflect_itinerary",
        _route_after_reflect,
        {"merge_results": "merge_results", "await_confirmation": "await_confirmation"},
    )
    graph.add_edge("await_confirmation", END)
    graph.add_edge("budget_agent", END)

    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
