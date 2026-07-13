"""Natural language CLI — describe your trip in plain English.

Usage:
    PYTHONPATH=src python3 scripts/nlcli.py
    make nlcli
"""

import asyncio
import uuid

from temporalio.client import Client

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow, ItineraryWorkflowInput
from smi_agent.graph.itinerary_graph import parse_intent
from smi_agent.graph.state import ItineraryState
from hitl_review import run_review_loop, wait_for_first_itinerary


async def main() -> None:
    print()
    print("Smartinerary — Natural Language Planner")
    print("─" * 40)
    print()
    print("Examples:")
    print('  "Fly from Chennai to Delhi on 2026-09-01, return 2026-09-05, budget £300"')
    print('  "Book a trip from Mumbai to Hyderabad 2026-10-10 to 2026-10-13"')
    print('  "Cheapest flights from Lucknow to Bengaluru on 2026-08-20, back 2026-08-25"')
    print()

    while True:
        goal = input("Describe your trip: ").strip()
        if goal:
            break
        print("  Please enter a trip description.")

    user_id = input("User ID [user-demo]: ").strip() or "user-demo"

    # ── Run parse_intent to extract structured fields ─────────────────────────
    plan_id = str(uuid.uuid4())
    dummy_state: ItineraryState = {
        "plan_id": plan_id,
        "tenant_id": "tenant-demo",
        "raw_goal": goal,
    }

    print()
    print("Parsing your request ...")
    parsed = await parse_intent(dummy_state)
    constraints = parsed.get("constraints", {})
    missing = parsed.get("needs_input", [])

    # ── Show what was understood ──────────────────────────────────────────────
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

    # ── Prompt for any missing required fields ────────────────────────────────
    if missing:
        print()
        print(f"Could not extract: {', '.join(missing)}")
        print("Please fill in the missing details:")
        print()

        field_prompts = {
            "origin":      "From (airport/city)   ",
            "destination": "To   (airport/city)   ",
            "check_in":    "Departure (YYYY-MM-DD)",
            "check_out":   "Return    (YYYY-MM-DD)",
        }
        for field in missing:
            value = input(f"  {field_prompts.get(field, field)}: ").strip()
            constraints[field] = value

    # ── Optional addon: override the derived sort preference ─────────────────
    override = input(
        f"\nOverride sort preference (currently '{constraints.get('sort_preference', 'cost')}')? "
        f"[cost/comfort/time, Enter to keep]: "
    ).strip().lower()
    if override in ("cost", "comfort", "time"):
        constraints["sort_preference"] = override

    # ── Final confirmation ────────────────────────────────────────────────────
    print()
    confirm = input("Submit this to Temporal? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelled.")
        return

    # ── Connect and start the workflow ────────────────────────────────────────
    # start_workflow (not execute_workflow) because HITL needs a handle to
    # signal/query while the workflow is paused in its review loop.
    print()
    print("Connecting to Temporal ...")
    client = await Client.connect("localhost:7233")
    print("Running workflow ...")
    print()

    handle = await client.start_workflow(
        ItineraryWorkflow.run,
        ItineraryWorkflowInput(
            plan_id=plan_id,
            tenant_id="tenant-demo",
            user_id=user_id,
            raw_goal=goal,
            origin=constraints.get("origin", ""),
            destination=constraints.get("destination", ""),
            check_in=constraints.get("check_in", ""),
            check_out=constraints.get("check_out", ""),
            sort_by=constraints.get("sort_preference", "cost"),
        ),
        id=f"nl-{plan_id}",
        task_queue="smartinerary",
    )

    itinerary = await wait_for_first_itinerary(handle)
    if itinerary is None:
        print("Timed out waiting for the itinerary to be generated.")
        print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/nl-{plan_id}")
        return

    await run_review_loop(handle, itinerary)

    print()
    print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/nl-{plan_id}")


if __name__ == "__main__":
    asyncio.run(main())
