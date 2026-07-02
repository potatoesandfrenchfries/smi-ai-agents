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
    print()
    print("Understood:")
    print(f"  From        : {constraints.get('origin') or '—'}")
    print(f"  To          : {constraints.get('destination') or '—'}")
    print(f"  Departure   : {constraints.get('check_in') or '—'}")
    print(f"  Return      : {constraints.get('check_out') or '—'}")
    print(f"  Budget      : {'£' + str(constraints.get('budget_gbp')) if constraints.get('budget_gbp') else '—'}")
    print(f"  Sort by     : {constraints.get('sort_preference', 'cost')}")

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

    # ── Final confirmation ────────────────────────────────────────────────────
    print()
    confirm = input("Submit this to Temporal? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelled.")
        return

    # ── Connect and run workflow ──────────────────────────────────────────────
    print()
    print("Connecting to Temporal ...")
    client = await Client.connect("localhost:7233")
    print("Running workflow ...")
    print()

    result = await client.execute_workflow(
        ItineraryWorkflow.run,
        ItineraryWorkflowInput(
            plan_id=plan_id,
            tenant_id="tenant-demo",
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

    # ── Print result ──────────────────────────────────────────────────────────
    print("=== ITINERARY ===")
    print(f"Status      : {result.status}")
    print(f"Policy      : {result.policy_status}")
    if result.total_cost_gbp:
        print(f"Total cost  : £{result.total_cost_gbp:.2f}")

    print()
    print("Segments:")
    for seg in result.segments:
        price = f"  £{seg['price_gbp']:.2f}" if seg.get("price_gbp") else ""
        print(f"  [{seg['type'].upper()}] {seg['summary']}{price}")
        print(f"           Provider : {seg['provider']}")
        print(f"           Book at  : {seg['handoff_link']}")

    if result.dining_options:
        print()
        print("Dining options:")
        for r in result.dining_options[:3]:
            print(f"  {r.get('name')} — {r.get('cuisine')} — {r.get('price_band')}  ({r.get('highlight', '')})")

    if result.assumptions:
        print()
        print("Assumptions:")
        for a in result.assumptions:
            print(f"  - {a}")

    if result.errors:
        print()
        print("Errors:", result.errors)

    print()
    print("Awaiting your confirmation before any booking is made.")
    print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/nl-{plan_id}")


if __name__ == "__main__":
    asyncio.run(main())
