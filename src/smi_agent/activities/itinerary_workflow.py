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
    from smi_agent.activities.task_queues import (
        ATTRACTION_QUEUE,
        CORE_SERVICES_QUEUE,
        FLIGHT_QUEUE,
        HOTEL_QUEUE,
        ITINERARY_GENERATION_QUEUE,
        RESTAURANT_QUEUE,
    )
    from smi_agent.activities.travel_activities import (
        AttractionSearchParams,
        FlightSearchParams,
        HotelSearchParams,
        ItineraryParams,
        ItineraryResult,
        PersistTripParams,
        RankingFeedbackEvent,
        RankingFeedbackParams,
        RestaurantSearchParams,
        TripIntentParams,
        TripIntentResult,
        WorkflowMetricParams,
        attraction_search_activity,
        flight_search_activity,
        hotel_search_activity,
        itinerary_generation_activity,
        parse_trip_intent_activity,
        persist_trip_activity,
        record_ranking_feedback_activity,
        record_workflow_metric_activity,
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
    # Callers that already collected structured trip fields (e.g. scripts/demo.py)
    # can pass these directly. Callers with only free text (e.g. the gateway's
    # POST /api/v1/trips) leave them unset — run() then resolves them itself via
    # parse_trip_intent_activity before dispatching any search activity.
    origin: str = ""
    destination: str = ""
    check_in: str = ""
    check_out: str = ""
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
    quality_review: dict = field(default_factory=dict)  # reflect_itinerary's critic findings
    decision_log: list[dict] = field(default_factory=list)  # Selected vs. rejected candidates per section, with reasons


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

    # ── Metrics ──────────────────────────────────────────────────────────────
    # Recorded via an activity, never inline — Temporal replays workflow code
    # from history, which would double-count a plain counter.inc() call here.

    # ── Agent-to-agent handoff (FR-ORC-4) ───────────────────────────────────
    # Read fresh from self._replies (not cached from the initial search) so
    # an edit that reorders the flight/hotel candidates — request_changes()
    # promotes a different one to the front — is reflected the next time
    # itinerary_generation_activity runs, not just on the very first pass.

    def _current_flight_arrival(self) -> str | None:
        flights = self._replies.get("flight") or []
        return flights[0].get("arrival") if flights else None

    def _current_hotel_name(self) -> str | None:
        hotels = self._replies.get("hotel") or []
        return hotels[0].get("name") if hotels else None

    async def _record_metric(self, status: str) -> None:
        await workflow.execute_activity(
            record_workflow_metric_activity,
            WorkflowMetricParams(workflow_name="ItineraryWorkflow", status=status),
            task_queue=CORE_SERVICES_QUEUE,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_default_retry(),
        )

    async def _record_ranking_feedback(
        self, user_id: str, tenant_id: str, events: list[RankingFeedbackEvent],
    ) -> None:
        """Feed HITL accept/reject signals back to providers/ranking/ so the
        bandit arm learns from them, regardless of which arm produced the
        recommendation being reacted to. Best-effort: a failure here should
        never take down the review flow the traveler is actually waiting on.
        """
        if not events:
            return
        try:
            await workflow.execute_activity(
                record_ranking_feedback_activity,
                RankingFeedbackParams(user_id=user_id, tenant_id=tenant_id, events=events),
                task_queue=CORE_SERVICES_QUEUE,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=_default_retry(),
            )
        except Exception as exc:
            workflow.logger.warning("Ranking feedback recording failed (non-fatal): %s", exc)

    @staticmethod
    def _itinerary_feedback_events(itinerary: ItineraryResult, action: str) -> list[RankingFeedbackEvent]:
        """One event per section that actually carries ranking attribution —
        segments/dining options with no candidate_id or rank_arm (e.g. a
        section with no results) are silently skipped rather than recorded
        with garbage data. Flight/hotel/attraction come from `segments`;
        restaurant isn't a segment (it's surfaced separately as
        `dining_options` — see compile_itinerary) so its top pick is read
        from there instead.
        """
        events: list[RankingFeedbackEvent] = []

        for seg in itinerary.segments:
            candidate_id = seg.get("candidate_id")
            arm = seg.get("rank_arm")
            if not candidate_id or not arm:
                continue
            events.append(RankingFeedbackEvent(
                section=seg["type"], candidate_id=candidate_id, action=action,
                arm=arm, features=seg.get("rank_features") or {},
                categorical=seg.get("rank_categorical") or {},
            ))

        if itinerary.dining_options:
            top = itinerary.dining_options[0]
            if top.get("id") and top.get("rank_arm"):
                events.append(RankingFeedbackEvent(
                    section="restaurant", candidate_id=top["id"], action=action,
                    arm=top["rank_arm"], features=top.get("rank_features") or {},
                    categorical=top.get("rank_categorical") or {},
                ))

        return events

    @workflow.run
    async def run(self, input: ItineraryWorkflowInput) -> ItineraryWorkflowResult:
        workflow.logger.info("Starting itinerary workflow for plan %s", input.plan_id)
        await self._record_metric("started")

        # ── Step 0: Resolve trip constraints when the caller only gave raw_goal ──
        # Callers with structured fields already (origin/destination/check_in/
        # check_out) skip this — those values are trusted as-is, same as before.
        # Callers with only free text (the gateway's POST /api/v1/trips collects
        # nothing else) need those fields resolved before any search activity can
        # run at all. Reuses the same LLM+regex extraction the LangGraph's
        # parse_intent node uses (resolve_trip_constraints in itinerary_graph.py)
        # via parse_trip_intent_activity, then threads the result through as
        # resolved_constraints/skip_reparse=True below so itinerary_generation_activity
        # doesn't redundantly re-parse raw_goal a second time.
        origin = input.origin
        destination = input.destination
        check_in = input.check_in
        check_out = input.check_out
        sort_by = input.sort_by
        resolved_constraints: dict | None = None

        if not (origin and destination and check_in and check_out):
            workflow.logger.info("No pre-resolved trip fields — parsing raw_goal via parse_trip_intent_activity")
            intent: TripIntentResult = await workflow.execute_activity(
                parse_trip_intent_activity,
                TripIntentParams(raw_goal=input.raw_goal),
                task_queue=ITINERARY_GENERATION_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            )
            if intent.missing_fields:
                workflow.logger.warning("Could not resolve required trip fields: %s", intent.missing_fields)
                await self._record_metric("error")
                return ItineraryWorkflowResult(
                    plan_id=input.plan_id,
                    status="error",
                    errors=[
                        "Could not determine "
                        + ", ".join(intent.missing_fields)
                        + " from the trip description — please include them explicitly."
                    ],
                )

            origin = origin or intent.origin or ""
            destination = destination or intent.destination or ""
            check_in = check_in or intent.check_in or ""
            check_out = check_out or intent.check_out or ""
            sort_by = intent.sort_preference or sort_by
            resolved_constraints = {
                "origin": origin,
                "destination": destination,
                "check_in": check_in,
                "check_out": check_out,
                "budget_gbp": intent.budget_gbp,
                "purpose": intent.purpose,
                "traveler_count": intent.traveler_count,
                "sort_preference": sort_by,
            }

        # ── Step 1: Search — staged as a real agent-to-agent pipeline
        # (FR-ORC-4), not a flat fan-out: flight resolves first and hands its
        # arrival time to hotel search below; the resolved hotel then hands
        # its location to restaurant/attraction search, which still run
        # concurrently with each other since neither depends on the other.
        # Temporal retries each activity independently per _default_retry();
        # a failure in one is isolated (try/except for the sequential legs,
        # return_exceptions=True for the concurrent pair) rather than taking
        # down the whole workflow — mirrors the isolation the LangGraph layer
        # does one level down.
        #
        # Attractions are always fetched alongside restaurants (mock data,
        # cheap) — whether they're actually shown depends on the
        # business/leisure classification the graph makes from raw_goal in
        # itinerary_generation_activity.
        workflow.logger.info("Dispatching search activities (flight -> hotel -> restaurant/attraction)...")

        # Flight search needs an IATA code; the OSM-backed hotel/restaurant/
        # attraction searches need a real place name — resolve both forms
        # once here rather than passing the same (likely IATA) destination
        # to every activity and having the OSM-based ones silently fail to
        # match anything real.
        destination_city = to_city_name(destination)
        destination_iata = to_iata(destination)

        errors: list[str] = []

        # Stage 1: flight (feeds hotel below)
        try:
            flights = await workflow.execute_activity(
                flight_search_activity,
                FlightSearchParams(
                    origin=to_iata(origin),
                    destination=destination_iata,
                    date=check_in,
                    sort_by=sort_by,
                ),
                task_queue=FLIGHT_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            )
        except Exception as exc:
            workflow.logger.error("flight_search_activity failed after retries: %s", exc)
            errors.append(f"flight_search: {exc}")
            flights = []

        # Stage 2: hotel, receives the flight's arrival time (feeds restaurant/attraction below)
        try:
            hotels = await workflow.execute_activity(
                hotel_search_activity,
                HotelSearchParams(
                    location=destination_city,
                    check_in=check_in,
                    check_out=check_out,
                    sort_by="rating" if sort_by == "comfort" else "price",
                ),
                task_queue=HOTEL_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            )
        except Exception as exc:
            workflow.logger.error("hotel_search_activity failed after retries: %s", exc)
            errors.append(f"hotel_search: {exc}")
            hotels = []

        # Stage 3: restaurant + attraction, receive the resolved hotel location
        restaurant_result, attraction_result = await asyncio.gather(
            workflow.execute_activity(
                restaurant_search_activity,
                RestaurantSearchParams(
                    location=destination_city,
                    cuisine=input.cuisine_preference,
                ),
                task_queue=RESTAURANT_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            workflow.execute_activity(
                attraction_search_activity,
                AttractionSearchParams(
                    location=destination_city,
                    sort_by="price" if sort_by == "cost" else "rating",
                ),
                task_queue=ATTRACTION_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
            return_exceptions=True,
        )
        restaurants = _unwrap_search_result(restaurant_result, "restaurant", errors)
        attractions = _unwrap_search_result(attraction_result, "attraction", errors)

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
                    user_id=input.user_id,
                    raw_goal=input.raw_goal,
                    origin=origin,
                    destination=destination,
                    check_in=check_in,
                    check_out=check_out,
                    sort_by=sort_by,
                    flights=flights,
                    hotels=hotels,
                    restaurants=restaurants,
                    attractions=attractions,
                    flight_arrival=self._current_flight_arrival(),
                    hotel_name=self._current_hotel_name(),
                    # Step 0 already resolved constraints from raw_goal when the
                    # caller only gave free text — trust them as-is here instead
                    # of having the graph's own parse_intent re-derive (and
                    # potentially diverge from) the same fields a second time.
                    resolved_constraints=resolved_constraints,
                    skip_reparse=resolved_constraints is not None,
                ),
                task_queue=ITINERARY_GENERATION_QUEUE,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_default_retry(),
            )
        except Exception as exc:
            workflow.logger.error("itinerary_generation_activity failed after retries: %s", exc)
            await self._record_metric("error")
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
                await self._record_metric("review_timed_out")
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
                await self._record_metric("rejected")
                await self._record_ranking_feedback(
                    input.user_id, input.tenant_id,
                    self._itinerary_feedback_events(self._itinerary, "rejected"),
                )
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

                    # A deliberate pick is an unambiguous positive signal —
                    # the traveler explicitly chose this candidate over
                    # whatever the compiled itinerary had, regardless of
                    # which arm produced either one.
                    picked = next((c for c in before if c.get("id") == edit.candidate_id), None)
                    if picked is not None and picked.get("rank_arm"):
                        await self._record_ranking_feedback(
                            input.user_id, input.tenant_id,
                            [RankingFeedbackEvent(
                                section=edit.section, candidate_id=edit.candidate_id, action="accepted",
                                arm=picked["rank_arm"], features=picked.get("rank_features") or {},
                                categorical=picked.get("rank_categorical") or {},
                            )],
                        )

                try:
                    self._itinerary = await workflow.execute_activity(
                        itinerary_generation_activity,
                        ItineraryParams(
                            plan_id=input.plan_id,
                            tenant_id=input.tenant_id,
                            user_id=input.user_id,
                            raw_goal=input.raw_goal,
                            flights=self._replies.get("flight", []),
                            hotels=self._replies.get("hotel", []),
                            restaurants=self._replies.get("restaurant", []),
                            attractions=self._replies.get("attraction", []),
                            flight_arrival=self._current_flight_arrival(),
                            hotel_name=self._current_hotel_name(),
                            resolved_constraints=self._constraints,
                            skip_reparse=True,
                        ),
                        task_queue=ITINERARY_GENERATION_QUEUE,
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
            quality_review=self._itinerary.quality_review,
            decision_log=self._itinerary.decision_log,
        )

        await self._record_metric("confirmed")
        await self._record_ranking_feedback(
            input.user_id, input.tenant_id,
            self._itinerary_feedback_events(self._itinerary, "accepted"),
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
                origin=origin,
                destination=destination_city,
                check_in=check_in,
                check_out=check_out,
                segments=final_result.segments,
                dining_options=final_result.dining_options,
                total_cost_gbp=final_result.total_cost_gbp,
                policy_status=final_result.policy_status,
                assumptions=final_result.assumptions,
                errors=final_result.errors,
                budget_alternatives=final_result.budget_alternatives,
            ),
            task_queue=CORE_SERVICES_QUEUE,
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
