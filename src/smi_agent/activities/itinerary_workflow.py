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
    ├── flight_search_activity      ┐
    ├── hotel_search_activity       ├─ parallel (asyncio.gather)
    ├── restaurant_search_activity  │
    └── attraction_search_activity  ┘
                │
                └── itinerary_generation_activity  (sequential — needs search results)
                            │
                            ▼
                  ── HITL review loop (FR-GAT-3, FR-PRS-2) ──
                  Workflow pauses here via wait_condition and
                  waits for a signal:
                    confirm()          → proceed, return final result
                    reject()           → end, status="rejected"
                    request_changes()  → reorder one section's candidates
                                         (or override budget_gbp), re-run
                                         itinerary_generation_activity with
                                         skip_reparse=True so ONLY
                                         merge/policy/compile re-run — the
                                         search activities are NOT re-invoked,
                                         satisfying "modify only the requested
                                         section without regenerating the
                                         entire itinerary". Then loops back
                                         to waiting.
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
        PersistTripParams,
        RestaurantSearchParams,
        attraction_search_activity,
        flight_search_activity,
        hotel_search_activity,
        itinerary_generation_activity,
        persist_trip_activity,
        restaurant_search_activity,
    )
    from smi_agent.examples.travel.tools.location_resolver import to_city_name, to_iata


# ── Workflow input / output ───────────────────────────────────────────────────

@dataclass
class ItineraryWorkflowInput:
    plan_id: str
    tenant_id: str
    user_id: str
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
    status: str  # "confirmed" | "rejected" | "review_timed_out" | "error" | "needs_approval"
    segments: list[dict] = field(default_factory=list)
    dining_options: list[dict] = field(default_factory=list)
    total_cost_gbp: float | None = None
    policy_status: str = "pending"
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_alternatives: list[dict] = field(default_factory=list)  # Budget Agent output on breach


@dataclass
class ItineraryEditRequest:
    """A targeted, single-section edit signalled by the traveler during review.

    Exactly one of candidate_id / budget_gbp is meaningful, depending on section:
      section="budget"                        → budget_gbp is the new limit
      section in flight/hotel/restaurant/attraction → candidate_id picks which
        already-fetched candidate to promote to "chosen" for that section
    """
    section: str  # "flight" | "hotel" | "restaurant" | "attraction" | "budget"
    candidate_id: str | None = None
    budget_gbp: float | None = None


_EDITABLE_SECTIONS = {"flight", "hotel", "restaurant", "attraction", "budget"}


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
                user_id="user-123",
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

    def __init__(self) -> None:
        self._itinerary: ItineraryResult | None = None
        self._constraints: dict = {}
        self._replies: dict[str, list[dict]] = {}
        self._confirmed = False
        self._rejected = False
        self._pending_edit: ItineraryEditRequest | None = None
        self._edit_log: list[str] = []

    # ── Signals (traveler → workflow) ───────────────────────────────────────

    @workflow.signal
    async def confirm(self) -> None:
        self._confirmed = True

    @workflow.signal
    async def reject(self) -> None:
        self._rejected = True

    @workflow.signal
    async def request_changes(self, edit: ItineraryEditRequest) -> None:
        if edit.section not in _EDITABLE_SECTIONS:
            workflow.logger.warning("Ignoring edit for unknown section: %s", edit.section)
            return
        self._pending_edit = edit

    # ── Queries (traveler ← workflow, read-only) ────────────────────────────

    @workflow.query
    def current_itinerary(self) -> ItineraryResult | None:
        """The itinerary as of the last completed generation/edit."""
        return self._itinerary

    @workflow.query
    def available_options(self) -> dict[str, list[dict]]:
        """Full candidate lists per section, for the traveler to pick from
        when requesting a change (not just the single "best" one shown in
        the compiled itinerary's segments).
        """
        return self._replies

    @workflow.query
    def edit_log(self) -> list[str]:
        return self._edit_log

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

        # Flight search needs an IATA code; the OSM-backed hotel/restaurant/
        # attraction searches need a real place name — resolve both forms
        # once here rather than passing the same (likely IATA) destination
        # to every activity and having the OSM-based ones silently fail to
        # match anything real.
        destination_city = to_city_name(input.destination)
        destination_iata = to_iata(input.destination)

        search_results = await asyncio.gather(
            workflow.execute_activity(
                flight_search_activity,
                FlightSearchParams(
                    origin=to_iata(input.origin),
                    destination=destination_iata,
                    date=input.check_in,
                    sort_by=input.sort_by,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            workflow.execute_activity(
                hotel_search_activity,
                HotelSearchParams(
                    location=destination_city,
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
                    location=destination_city,
                    cuisine=input.cuisine_preference,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            workflow.execute_activity(
                attraction_search_activity,
                AttractionSearchParams(
                    location=destination_city,
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

        # Full candidate lists per section, kept on the workflow instance so a
        # later request_changes() signal can pick a different one without
        # re-invoking any search activity.
        self._replies = {
            "flight": flights, "hotel": hotels,
            "restaurant": restaurants, "attraction": attractions,
        }

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

        self._itinerary = itinerary
        self._constraints = itinerary.resolved_constraints

        workflow.logger.info(
            "Itinerary ready for review — status: %s, policy: %s",
            itinerary.status, itinerary.policy_status,
        )

        # ── Step 3: HITL review loop (FR-GAT-3, FR-PRS-2) ───────────────────────
        # Pause here — durably, Temporal persists this wait — until the traveler
        # confirms, rejects, or requests a targeted edit. An edit reorders just
        # that section's already-fetched candidates (or overrides budget_gbp)
        # and re-runs itinerary_generation_activity with skip_reparse=True, so
        # only merge_results/policy_check/compile_itinerary redo work — the
        # search activities are never re-invoked. Loops until confirmed/rejected
        # or the review window elapses.
        review_deadline = timedelta(hours=24)
        while True:
            try:
                await workflow.wait_condition(
                    lambda: self._confirmed or self._rejected or self._pending_edit is not None,
                    timeout=review_deadline,
                )
            except TimeoutError:
                workflow.logger.warning("Review window elapsed with no traveler response")
                return ItineraryWorkflowResult(
                    plan_id=input.plan_id,
                    status="review_timed_out",
                    segments=self._itinerary.segments,
                    total_cost_gbp=self._itinerary.total_cost_gbp,
                    policy_status=self._itinerary.policy_status,
                    errors=errors + ["No confirmation or edit received within the review window"],
                )

            if self._rejected:
                workflow.logger.info("Itinerary rejected by traveler")
                return ItineraryWorkflowResult(plan_id=input.plan_id, status="rejected", errors=errors)

            if self._pending_edit is not None:
                edit = self._pending_edit
                self._pending_edit = None
                workflow.logger.info("Applying edit: section=%s candidate_id=%s budget_gbp=%s",
                                      edit.section, edit.candidate_id, edit.budget_gbp)

                if edit.section == "budget" and edit.budget_gbp is not None:
                    self._constraints = {**self._constraints, "budget_gbp": edit.budget_gbp}
                    self._edit_log.append(f"budget → £{edit.budget_gbp:.2f}")
                elif edit.candidate_id is not None:
                    before = self._replies.get(edit.section, [])
                    self._replies[edit.section] = _reorder_to_front(before, edit.candidate_id)
                    self._edit_log.append(f"{edit.section} → {edit.candidate_id}")

                try:
                    self._itinerary = await workflow.execute_activity(
                        itinerary_generation_activity,
                        ItineraryParams(
                            plan_id=input.plan_id,
                            tenant_id=input.tenant_id,
                            raw_goal=input.raw_goal,
                            flights=self._replies.get("flight", []),
                            hotels=self._replies.get("hotel", []),
                            restaurants=self._replies.get("restaurant", []),
                            attractions=self._replies.get("attraction", []),
                            resolved_constraints=self._constraints,
                            skip_reparse=True,
                        ),
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=_default_retry(),
                    )
                    self._constraints = self._itinerary.resolved_constraints or self._constraints
                except Exception as exc:
                    workflow.logger.error("Edit re-run failed: %s", exc)
                    errors.append(f"edit_apply: {exc}")
                continue

            if self._confirmed:
                break

        workflow.logger.info("Itinerary confirmed by traveler")
        final_result = ItineraryWorkflowResult(
            plan_id=input.plan_id,
            status="confirmed",
            segments=self._itinerary.segments,
            dining_options=self._itinerary.dining_options,
            total_cost_gbp=self._itinerary.total_cost_gbp,
            policy_status=self._itinerary.policy_status,
            assumptions=self._itinerary.assumptions,
            errors=errors + self._itinerary.errors,
            budget_alternatives=self._itinerary.budget_alternatives,
        )

        # Persist so this trip can be looked up from a later, unrelated
        # conversation (e.g. a NYC trip started after this Japan one) —
        # file-backed for now, Postgres-backed later, via PersistTripParams.
        await workflow.execute_activity(
            persist_trip_activity,
            PersistTripParams(
                trip_id=input.plan_id,
                user_id=input.user_id,
                tenant_id=input.tenant_id,
                status=final_result.status,
                origin=input.origin,
                destination=destination_city,
                check_in=input.check_in,
                check_out=input.check_out,
                segments=final_result.segments,
                dining_options=final_result.dining_options,
                total_cost_gbp=final_result.total_cost_gbp,
                policy_status=final_result.policy_status,
                assumptions=final_result.assumptions,
                errors=final_result.errors,
                budget_alternatives=final_result.budget_alternatives,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_default_retry(),
        )

        return final_result


# ── Edit application ──────────────────────────────────────────────────────────

def _reorder_to_front(candidates: list[dict], candidate_id: str) -> list[dict]:
    """Promote the traveler's chosen candidate to the front of the list.

    merge_results/compile_itinerary always treat candidates[0] as "the pick",
    so reordering is all a section edit needs — no re-fetch, no data loss (the
    other candidates stay available for a later edit). No-ops if the id isn't
    found, leaving the original order untouched.
    """
    return sorted(candidates, key=lambda c: c.get("id") != candidate_id)


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
