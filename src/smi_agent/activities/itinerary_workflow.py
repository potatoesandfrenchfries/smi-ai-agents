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
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    # Imports here are only resolved at worker startup, not during workflow replay.
    # Putting them inside the unsafe context prevents Temporal's sandbox from
    # blocking standard library imports that are safe but not deterministic.
    from smi_agent.activities.travel_activities import (
        AttractionSearchParams,
        FlightSearchParams,
        HotelSearchParams,
        ItineraryParams,
        ItineraryResult,
        RestaurantSearchParams,
        attraction_search_activity,
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

        # ── Step 1: Fan out to all four search activities in parallel ───────────
        # All four run concurrently. Total latency = slowest activity, not sum.
        # Temporal retries each independently per _default_retry(); if one still
        # exhausts its retries, return_exceptions=True stops that single failure
        # from taking down the others (and the whole workflow) with it — mirrors
        # the isolation the LangGraph layer does one level down.
        #
        # Attractions are always fetched alongside the rest (mock data, cheap) —
        # whether they're actually shown depends on the business/leisure
        # classification the graph makes from raw_goal in itinerary_generation_activity.
        workflow.logger.info("Dispatching parallel search activities...")

        search_results = await asyncio.gather(
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
            workflow.execute_activity(
                attraction_search_activity,
                AttractionSearchParams(
                    location=input.destination,
                    sort_by="price" if input.sort_by == "cost" else "rating",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            return_exceptions=True,
        )

        errors: list[str] = []
        flights, hotels, restaurants, attractions = (
            _unwrap_search_result(result, name, errors)
            for result, name in zip(search_results, ("flight", "hotel", "restaurant", "attraction"))
        )

        workflow.logger.info(
            "Search complete — flights: %d, hotels: %d, restaurants: %d, attractions: %d",
            len(flights), len(hotels), len(restaurants), len(attractions),
        )
        if errors:
            workflow.logger.warning("Search errors (continuing with partial results): %s", errors)

        # ── Step 2: Generate the itinerary from search results ─────────────────
        # Sequential — needs all three search results to compile a coherent plan.
        # Unlike the search fan-out, there's nothing to isolate a failure from
        # here — if this activity exhausts its retries, catch it and return a
        # typed error result instead of letting an ActivityError escape run()
        # as an uncaught workflow failure.
        try:
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
                    attractions=attractions,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_default_retry(),
            )
        except Exception as exc:
            workflow.logger.error("itinerary_generation_activity failed after retries: %s", exc)
            return ItineraryWorkflowResult(
                plan_id=input.plan_id,
                status="error",
                errors=errors + [f"itinerary_generation: {exc}"],
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
            errors=errors + itinerary.errors,
            budget_alternatives=itinerary.budget_alternatives,
        )


# ── Exception handling ────────────────────────────────────────────────────────

def _unwrap_search_result(result: Any, name: str, errors: list[str]) -> list[dict]:
    """Convert one gathered activity result (value or exception) into a list.

    Isolates a single search activity's exhausted-retries failure from the
    other two — the workflow degrades to partial results instead of failing
    outright, with the failure recorded in errors for the caller to see.
    """
    if isinstance(result, BaseException):
        workflow.logger.error("%s_search_activity failed after retries: %s", name, result)
        errors.append(f"{name}_search: {result}")
        return []
    return result


# ── Retry policy ──────────────────────────────────────────────────────────────

def _default_retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=3,
    )
