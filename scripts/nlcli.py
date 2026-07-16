"""Natural language CLI — describe your trip in plain English.

Usage:
    PYTHONPATH=src python3 scripts/nlcli.py
    make nlcli
"""

import asyncio
import uuid

from hitl_review import describe_candidate, run_review_loop, wait_for_first_itinerary
from temporalio.client import Client

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow, ItineraryWorkflowInput
from smi_agent.graph.intent_classifier import Intent, IntentClassifierAgent
from smi_agent.graph.itinerary_graph import (
    budget_agent,
    parse_intent,
    run_attraction_search,
    run_flight_search,
    run_hotel_search,
    run_restaurant_search,
)
from smi_agent.graph.state import ItineraryState, TaskRequest
from smi_agent.trip_store import InProgressPlan, InProgressPlanStore

TENANT_ID = "tenant-demo"


async def _extract_constraints(goal: str, required: list[str]) -> dict:
    """Parse the goal into constraints and interactively fill in any of the
    caller's required fields that couldn't be extracted (subset of
    parse_intent.REQUIRED_FIELDS, since a flight-only search doesn't need a
    budget and a restaurant-only search doesn't need dates).
    """
    dummy_state: ItineraryState = {
        "plan_id": str(uuid.uuid4()),
        "tenant_id": TENANT_ID,
        "raw_goal": goal,
    }
    parsed = await parse_intent(dummy_state)
    constraints = parsed.get("constraints", {})

    field_prompts = {
        "origin":      "From (airport/city)   ",
        "destination": "To   (airport/city)   ",
        "check_in":    "Departure (YYYY-MM-DD)",
        "check_out":   "Return    (YYYY-MM-DD)",
    }
    missing = [f for f in required if not constraints.get(f)]
    if missing:
        print()
        print(f"Could not extract: {', '.join(missing)}")
        for field in missing:
            value = input(f"  {field_prompts.get(field, field)}: ").strip()
            constraints[field] = value

    return constraints


async def run_quick_search(intent: Intent, goal: str) -> None:
    """One-off search for a single domain — no Temporal workflow, no HITL,
    just the matching provider called directly and the results printed.
    """
    task = TaskRequest(
        task_id=str(uuid.uuid4()), goal=goal, constraints={}, context_ref="quick-search",
        deadline_seconds=30, budget_remaining_usd=0.50,
    )

    if intent is Intent.FLIGHT_SEARCH:
        constraints = await _extract_constraints(goal, ["origin", "destination", "check_in"])
        task["constraints"] = constraints
        print()
        print(f"Searching flights {constraints.get('origin')} → {constraints.get('destination')} on {constraints.get('check_in')} ...")
        reply = await run_flight_search(task, constraints)
        section = "flight"
    elif intent is Intent.HOTEL_SEARCH:
        constraints = await _extract_constraints(goal, ["destination", "check_in", "check_out"])
        task["constraints"] = constraints
        print()
        print(f"Searching hotels in {constraints.get('destination')} ({constraints.get('check_in')} to {constraints.get('check_out')}) ...")
        reply = await run_hotel_search(task, constraints)
        section = "hotel"
    else:
        constraints = await _extract_constraints(goal, ["destination"])
        task["constraints"] = constraints
        print()
        print(f"Searching restaurants in {constraints.get('destination')} ...")
        reply = await run_restaurant_search(task, constraints)
        section = "restaurant"

    candidates = reply.get("candidates", [])
    if not candidates:
        print("  No results found.")
        return
    print()
    for i, c in enumerate(candidates, start=1):
        print(f"  {i}. {describe_candidate(section, c)}")


async def run_budget_query(goal: str, user_id: str, client: Client) -> None:
    """Answer a budget question either against an active in-progress plan
    (reconnect + reuse budget_agent's cheaper-alternatives logic) or, if
    there's no active plan, as a quick standalone cost estimate.
    """
    plans = await InProgressPlanStore().list_for_user(user_id)
    if plans:
        plan = plans[0]
        try:
            handle = client.get_workflow_handle(plan.workflow_id)
            itinerary = await handle.query(ItineraryWorkflow.current_itinerary)
            options = await handle.query(ItineraryWorkflow.available_options)
        except Exception:
            print("  Could not reconnect to your active plan — it may have already finished.")
            await InProgressPlanStore().delete(user_id, plan.plan_id)
            plans = []
        else:
            print()
            print(f"Checking budget for your active plan ({plan.origin} → {plan.destination}) ...")
            state = {
                "constraints": {"budget_gbp": (itinerary.resolved_constraints or {}).get("budget_gbp")},
                "trip_type": "leisure",
                "total_cost_gbp": itinerary.total_cost_gbp,
                "flight_reply": {"candidates": options.get("flight", [])},
                "hotel_reply": {"candidates": options.get("hotel", [])},
                "restaurant_reply": {"candidates": options.get("restaurant", [])},
                "attraction_reply": {"candidates": options.get("attraction", [])},
            }
            result = await budget_agent(state)
            alternatives = result.get("budget_alternatives", [])
            print(f"  Current total: £{itinerary.total_cost_gbp:.2f}" if itinerary.total_cost_gbp else "  Current total: unknown")
            if alternatives:
                print("  Cheaper alternatives:")
                for i, alt in enumerate(alternatives, start=1):
                    fit = "within budget" if alt.get("within_budget") else "still over budget"
                    print(f"    {i}. {alt['label']} — £{alt['total_cost_gbp']:.2f} (saves £{alt['savings_gbp']:.2f}, {fit})")
            else:
                print("  No cheaper alternatives found among the already-fetched options.")
            return

    print()
    print("No active plan found — estimating cost for a new trip instead.")
    constraints = await _extract_constraints(goal, ["origin", "destination", "check_in", "check_out"])
    task = TaskRequest(
        task_id=str(uuid.uuid4()), goal=goal, constraints=constraints, context_ref="quick-search",
        deadline_seconds=30, budget_remaining_usd=0.50,
    )
    flight, hotel, restaurant, attraction = await asyncio.gather(
        run_flight_search(task, constraints),
        run_hotel_search(task, constraints),
        run_restaurant_search(task, constraints),
        run_attraction_search(task, constraints),
    )
    best_flight = flight["candidates"][0] if flight["candidates"] else {}
    best_hotel = hotel["candidates"][0] if hotel["candidates"] else {}
    best_restaurants = restaurant["candidates"][:3]
    total = (
        (best_flight.get("price_gbp") or 0)
        + (best_hotel.get("total_price_gbp") or 0)
        + sum(r.get("avg_spend_per_person_gbp") or 0 for r in best_restaurants)
    )
    budget = constraints.get("budget_gbp")
    print()
    print(f"  Estimated total: £{total:.2f}")
    if budget:
        fit = "within" if total <= budget else "over"
        print(f"  Your budget of £{budget:.2f} is {fit} this estimate.")


async def run_modify(goal: str, user_id: str, client: Client) -> bool:
    """Reconnect to the traveler's most recent still-open plan and resume the
    review loop. Returns False (fall back to generate_itinerary) if no active
    plan exists or reconnecting fails.
    """
    plans = await InProgressPlanStore().list_for_user(user_id)
    if not plans:
        print()
        print("No active plan found to modify — starting a new one instead.")
        return False

    plan = plans[0]
    try:
        handle = client.get_workflow_handle(plan.workflow_id)
        itinerary = await handle.query(ItineraryWorkflow.current_itinerary)
        if itinerary is None:
            raise RuntimeError("plan has no itinerary yet")
    except Exception:
        print()
        print("Could not reconnect to your active plan — it may have already finished. Starting a new one instead.")
        await InProgressPlanStore().delete(user_id, plan.plan_id)
        return False

    print()
    print(f"Reconnected to your active plan: {plan.origin} → {plan.destination}")
    await run_review_loop(handle, itinerary)
    await InProgressPlanStore().delete(user_id, plan.plan_id)
    return True


async def run_generate(goal: str, user_id: str, client: Client) -> None:
    """The original flow: parse constraints, prompt for anything missing,
    submit to Temporal, and drive the HITL review loop.
    """
    plan_id = str(uuid.uuid4())
    constraints = await _extract_constraints(goal, ["origin", "destination", "check_in", "check_out"])

    purpose = constraints.get("purpose", "leisure")
    print()
    print("Understood:")
    print(f"  From        : {constraints.get('origin') or '—'}")
    print(f"  To          : {constraints.get('destination') or '—'}")
    print(f"  Departure   : {constraints.get('check_in') or '—'}")
    print(f"  Return      : {constraints.get('check_out') or '—'}")
    print(f"  Budget      : {'£' + str(constraints.get('budget_gbp')) if constraints.get('budget_gbp') else '—'}")
    print(f"  Trip type   : {purpose.title()}")
    print(f"  Sort by     : {constraints.get('sort_preference', 'cost')}  (derived from trip type, not parsed directly)")

    override = input(
        f"\nOverride sort preference (currently '{constraints.get('sort_preference', 'cost')}')? "
        f"[cost/comfort/time, Enter to keep]: "
    ).strip().lower()
    if override in ("cost", "comfort", "time"):
        constraints["sort_preference"] = override

    print()
    confirm = input("Submit this to Temporal? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelled.")
        return

    print()
    print("Running workflow ...")
    print()

    workflow_id = f"nl-{plan_id}"
    handle = await client.start_workflow(
        ItineraryWorkflow.run,
        ItineraryWorkflowInput(
            plan_id=plan_id,
            tenant_id=TENANT_ID,
            user_id=user_id,
            raw_goal=goal,
            origin=constraints.get("origin", ""),
            destination=constraints.get("destination", ""),
            check_in=constraints.get("check_in", ""),
            check_out=constraints.get("check_out", ""),
            sort_by=constraints.get("sort_preference", "cost"),
        ),
        id=workflow_id,
        task_queue="smartinerary",
    )

    await InProgressPlanStore().save(InProgressPlan(
        plan_id=plan_id,
        workflow_id=workflow_id,
        user_id=user_id,
        tenant_id=TENANT_ID,
        raw_goal=goal,
        origin=constraints.get("origin", ""),
        destination=constraints.get("destination", ""),
    ))

    itinerary = await wait_for_first_itinerary(handle)
    if itinerary is None:
        print("Timed out waiting for the itinerary to be generated.")
        print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/{workflow_id}")
        return

    await run_review_loop(handle, itinerary)
    await InProgressPlanStore().delete(user_id, plan_id)

    print()
    print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/{workflow_id}")


async def main() -> None:
    print()
    print("Smartinerary — Natural Language Planner")
    print("─" * 40)
    print()
    print("Examples:")
    print('  "Fly from Chennai to Delhi on 2026-09-01, return 2026-09-05, budget £300"')
    print('  "Just show me flights from Mumbai to Hyderabad on 2026-10-10"')
    print('  "Change my hotel to something cheaper"')
    print('  "How much would a week in Tokyo cost?"')
    print()

    while True:
        goal = input("Describe your trip: ").strip()
        if goal:
            break
        print("  Please enter a trip description.")

    user_id = input("User ID [user-demo]: ").strip() or "user-demo"

    print()
    print("Understanding your request ...")
    result = await IntentClassifierAgent().run(goal)
    print(f"Detected intent: {result.intent.value} (confidence {result.confidence:.2f}) — {result.reasoning}")

    if result.intent in (Intent.FLIGHT_SEARCH, Intent.HOTEL_SEARCH, Intent.RESTAURANT_SEARCH):
        await run_quick_search(result.intent, goal)
        return

    print()
    print("Connecting to Temporal ...")
    client = await Client.connect("localhost:7233")

    if result.intent is Intent.BUDGET_QUERY:
        await run_budget_query(goal, user_id, client)
        return

    if result.intent is Intent.MODIFY_ITINERARY:
        handled = await run_modify(goal, user_id, client)
        if handled:
            return
        # Falls through to generate_itinerary with the same goal.

    await run_generate(goal, user_id, client)


if __name__ == "__main__":
    asyncio.run(main())
