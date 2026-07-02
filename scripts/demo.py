"""Demo script — submit an itinerary workflow and print the result.

Usage:
    PYTHONPATH=src python3 scripts/demo.py
    make demo
"""

import asyncio
import uuid

from temporalio.client import Client

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow, ItineraryWorkflowInput


async def main() -> None:
    client = await Client.connect("localhost:7233")
    print("Connected to Temporal")

    input = ItineraryWorkflowInput(
        plan_id=str(uuid.uuid4()),
        tenant_id="tenant-demo",
        raw_goal="Fly from EDI to CDG 2026-08-10 to 2026-08-14 budget 2000",
        origin="EDI",
        destination="CDG",
        check_in="2026-08-10",
        check_out="2026-08-14",
        sort_by="cost",
    )

    print(f"Submitting workflow for plan {input.plan_id} ...")
    result = await client.execute_workflow(
        ItineraryWorkflow.run,
        input,
        id=f"demo-{uuid.uuid4()}",
        task_queue="smartinerary",
    )

    print()
    print("=== ITINERARY RESULT ===")
    print(f"Status      : {result.status}")
    print(f"Policy      : {result.policy_status}")
    print(f"Total cost  : £{result.total_cost_gbp}")
    print("Segments:")
    for seg in result.segments:
        print(f"  [{seg['type']}] {seg['summary']} — {seg['provider']}")
    print(f"Dining      : {len(result.dining_options)} options")
    if result.assumptions:
        print(f"Assumptions : {result.assumptions}")
    if result.errors:
        print(f"Errors      : {result.errors}")


if __name__ == "__main__":
    asyncio.run(main())
