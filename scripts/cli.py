"""Interactive CLI — plan a trip by typing in the terminal.

Usage:
    PYTHONPATH=src python3 scripts/cli.py
    make cli
"""

import asyncio
import uuid

from temporalio.client import Client

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow, ItineraryWorkflowInput
from hitl_review import run_review_loop, wait_for_first_itinerary


def prompt(label: str, default: str = "") -> str:
    value = input(f"  {label}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def prompt_float(label: str, default: str = "") -> float | None:
    raw = prompt(label, default)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


async def main() -> None:
    print()
    print("Smartinerary — Itinerary Planner")
    print("─" * 36)

    # ── Gather inputs ─────────────────────────────────────────────────────────
    print()
    print("Enter your trip details (press Enter to use defaults):")
    print()

    user_id     = prompt("User ID                        ", "user-demo")
    origin      = prompt("Origin airport or city         ", "EDI")
    destination = prompt("Destination airport or city    ", "CDG")
    check_in    = prompt("Departure date (YYYY-MM-DD)    ", "2026-08-10")
    check_out   = prompt("Return date    (YYYY-MM-DD)    ", "2026-08-14")
    budget      = prompt_float("Budget (£, optional)          ")
    sort_by     = prompt("Sort by [cost / comfort / time]", "cost")

    if sort_by not in ("cost", "comfort", "time"):
        print(f"  Unknown sort '{sort_by}', defaulting to 'cost'")
        sort_by = "cost"

    budget_str = f" budget £{budget:.0f}" if budget else ""
    raw_goal = (
        f"Fly from {origin} to {destination} "
        f"{check_in} to {check_out}{budget_str}"
    )

    plan_id = str(uuid.uuid4())

    print()
    print(f"Plan   : {raw_goal}")
    print(f"Plan ID: {plan_id}")
    print()

    confirm = input("Submit to Temporal? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelled.")
        return

    # ── Connect and start the workflow ────────────────────────────────────────
    # start_workflow (not execute_workflow) because HITL needs a handle to
    # signal/query while the workflow is paused in its review loop.
    print()
    print("Connecting to Temporal ...")
    client = await Client.connect("localhost:7233")

    print("Running workflow (flight + hotel + restaurant + attraction searches in parallel) ...")
    print()

    handle = await client.start_workflow(
        ItineraryWorkflow.run,
        ItineraryWorkflowInput(
            plan_id=plan_id,
            tenant_id="tenant-demo",
            user_id=user_id,
            raw_goal=raw_goal,
            origin=origin,
            destination=destination,
            check_in=check_in,
            check_out=check_out,
            sort_by=sort_by,
        ),
        id=f"cli-{plan_id}",
        task_queue="smartinerary",
    )

    itinerary = await wait_for_first_itinerary(handle)
    if itinerary is None:
        print("Timed out waiting for the itinerary to be generated.")
        print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/cli-{plan_id}")
        return

    await run_review_loop(handle, itinerary)

    print()
    print(f"Temporal UI: http://localhost:8233/namespaces/default/workflows/cli-{plan_id}")


if __name__ == "__main__":
    asyncio.run(main())
