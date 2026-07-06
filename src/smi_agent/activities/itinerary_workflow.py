"""Temporal workflow for stateful itinerary generation.

Why Temporal makes this stateful
─────────────────────────────────
Without Temporal, if the server crashes mid-plan the entire request is lost.
Temporal persists every workflow step in its own database. When the worker
restarts it replays the workflow history up to where it failed and continues
from there — completed activities are never re-executed.

Workflow rules (enforced by Temporal)
──────────────────────────────────────
Workflows must be deterministic: no I/O, no random, no datetime.now().
All I/O happens inside activities. The workflow only orchestrates — it
calls activities, awaits results, and decides what to do next.

Flow
────
  ItineraryWorkflow.run()
    │
    ├── flight_search_activity  ┐
    ├── hotel_search_activity   ├─ parallel (asyncio.gather)
    └── restaurant_search_activity┘
                │
                └── itinerary_generation_activity  (sequential — needs search results)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    # Imports here are only resolved at worker startup, not during workflow replay.
    # Putting them inside the unsafe context prevents Temporal's sandbox from
    # blocking standard library imports that are safe but not deterministic.
    from smi_agent.activities.travel_activities import (
        FlightSearchParams,
        HotelSearchParams,
        ItineraryParams,
        ItineraryResult,
        RestaurantSearchParams,
        flight_search_activity,
        hotel_search_activity,
        itinerary_generation_activity,
        restaurant_search_activity,
    )


# ── Workflow input / output ───────────────────────────────────────────────────

@dataclass
class ItineraryWorkflowInput:
    plan_id: str
    tenant_id: str
    raw_goal: str
    origin: str
    destination: str
    check_in: str
    check_out: str
    sort_by: str = "cost"
    cuisine_preference: str | None = None


@dataclass
class ItineraryWorkflowResult:
    plan_id: str
    status: str
    segments: list[dict] = field(default_factory=list)
    dining_options: list[dict] = field(default_factory=list)
    total_cost_gbp: float | None = None
    policy_status: str = "pending"
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_alternatives: list[dict] = field(default_factory=list)  # Budget Agent output on breach


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow.defn
class ItineraryWorkflow:
    """Stateful itinerary planning workflow.

    Temporal persists the execution state after every activity completes.
    If the worker crashes between activities, it resumes from the last
    completed step — not from the beginning.

    Usage::

        client = await Client.connect("localhost:7233")
        result = await client.execute_workflow(
            ItineraryWorkflow.run,
            ItineraryWorkflowInput(
                plan_id="plan-001",
                tenant_id="tenant-abc",
                raw_goal="Fly from EDI to CDG, 10-14 Aug, budget £2000",
                origin="EDI",
                destination="CDG",
                check_in="2026-08-10",
                check_out="2026-08-14",
            ),
            id="itinerary-plan-001",
            task_queue="smartinerary",
        )
    """

    @workflow.run
    async def run(self, input: ItineraryWorkflowInput) -> ItineraryWorkflowResult:
        workflow.logger.info("Starting itinerary workflow for plan %s", input.plan_id)

        # ── Step 1: Fan out to all three search activities in parallel ─────────
        # All three run concurrently. Total latency = slowest activity, not sum.
        # If one fails, Temporal retries it independently — others are not affected.
        workflow.logger.info("Dispatching parallel search activities...")

        flights, hotels, restaurants = await asyncio.gather(
            workflow.execute_activity(
                flight_search_activity,
                FlightSearchParams(
                    origin=input.origin,
                    destination=input.destination,
                    date=input.check_in,
                    sort_by=input.sort_by,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            workflow.execute_activity(
                hotel_search_activity,
                HotelSearchParams(
                    location=input.destination,
                    check_in=input.check_in,
                    check_out=input.check_out,
                    sort_by="rating" if input.sort_by == "comfort" else "price",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            workflow.execute_activity(
                restaurant_search_activity,
                RestaurantSearchParams(
                    location=input.destination,
                    cuisine=input.cuisine_preference,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
        )

        workflow.logger.info(
            "Search complete — flights: %d, hotels: %d, restaurants: %d",
            len(flights), len(hotels), len(restaurants),
        )

        # ── Step 2: Generate the itinerary from search results ─────────────────
        # Sequential — needs all three search results to compile a coherent plan.
        itinerary: ItineraryResult = await workflow.execute_activity(
            itinerary_generation_activity,
            ItineraryParams(
                plan_id=input.plan_id,
                tenant_id=input.tenant_id,
                raw_goal=input.raw_goal,
                origin=input.origin,
                destination=input.destination,
                check_in=input.check_in,
                check_out=input.check_out,
                sort_by=input.sort_by,
                flights=flights,
                hotels=hotels,
                restaurants=restaurants,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_default_retry(),
        )

        workflow.logger.info(
            "Itinerary ready — status: %s, policy: %s",
            itinerary.status, itinerary.policy_status,
        )

        return ItineraryWorkflowResult(
            plan_id=input.plan_id,
            status=itinerary.status,
            segments=itinerary.segments,
            dining_options=itinerary.dining_options,
            total_cost_gbp=itinerary.total_cost_gbp,
            policy_status=itinerary.policy_status,
            assumptions=itinerary.assumptions,
            errors=itinerary.errors,
            budget_alternatives=itinerary.budget_alternatives,
        )


# ── Retry policy ──────────────────────────────────────────────────────────────

def _default_retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=3,
    )
