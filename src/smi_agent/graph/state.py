"""ItineraryState and A2A envelope types for the itinerary planning graph.

Follows the A2A contract defined in the Smartinerary PRD (Section 6):
  - TaskRequest  : orchestrator → specialist
  - TaskReply    : specialist → orchestrator
  - TripConstraints : typed constraint set extracted from the raw goal (FR-INT-2)
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired

from typing_extensions import TypedDict


class TripConstraints(TypedDict):
    """Typed constraint set parsed from the traveler's natural-language goal."""
    origin: str | None           # Departure city or IATA code
    destination: str | None      # Arrival city or IATA code
    check_in: str | None         # YYYY-MM-DD
    check_out: str | None        # YYYY-MM-DD
    budget_gbp: float | None     # Total trip budget
    purpose: str | None          # "business" | "leisure"
    traveler_count: int          # Default 1
    sort_preference: str         # "cost" | "comfort" | "time" | "rating"


class TaskRequest(TypedDict):
    """A2A request envelope: orchestrator → specialist (PRD Section 6.1)."""
    task_id: str
    goal: str                    # What to satisfy, in domain terms
    constraints: TripConstraints
    context_ref: str             # Pointer to shared state — never a context dump (FR-ORC-3)
    deadline_seconds: int
    budget_remaining_usd: float


class TaskReply(TypedDict):
    """A2A reply envelope: specialist → orchestrator (PRD Section 6.2)."""
    status: Literal["done", "partial", "blocked", "needs_input"]
    candidates: list[dict]       # Ranked, verified options
    assumptions: list[str]       # What was inferred to proceed
    cost_usd: float              # Tokens + tool calls consumed
    confidence: float            # Specialist self-assessment 0.0–1.0
    provenance: list[str]        # PriceSnapshot IDs behind each option (FR-SPC-4)


class ItineraryState(TypedDict):
    """Shared mutable state passed between all itinerary graph nodes.

    Follows the same TypedDict pattern as ConversationState in
    src/smi_agent/conversation/state.py.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    plan_id: str                 # Unique plan identifier (idempotency key)
    tenant_id: str               # Derived from auth header — never from user input (FR-ORC security)

    # ── Stage 1 · Intent ──────────────────────────────────────────────────────
    raw_goal: str                # Original natural-language travel goal

    # ── Stage 2 · Intake ──────────────────────────────────────────────────────
    constraints: NotRequired[TripConstraints | None]
    needs_input: NotRequired[list[str]]   # Missing required fields (triggers FR-INT-3 prompt)
    trip_type: NotRequired[Literal["business", "leisure"]]  # Derived from constraints.purpose — drives graph routing

    # ── Stage 3 · Plan graph (FR-ORC-6) ──────────────────────────────────────
    plan_graph: NotRequired[dict]         # Decomposition + dispatch + merge record

    # ── Stage 4 · Specialist replies ──────────────────────────────────────────
    flight_reply: NotRequired[TaskReply | None]
    hotel_reply: NotRequired[TaskReply | None]
    restaurant_reply: NotRequired[TaskReply | None]
    attraction_reply: NotRequired[TaskReply | None]   # Leisure path only

    # ── Stage 5 · Merge + policy ──────────────────────────────────────────────
    policy_status: NotRequired[Literal["compliant", "breach", "pending"]]
    total_cost_gbp: NotRequired[float | None]

    # ── Stage 6 · Present + handoff ───────────────────────────────────────────
    itinerary: NotRequired[dict | None]       # Versioned itinerary with segments (FR-PRS-1)
    confirmation_status: NotRequired[         # HITL gate (FR-GAT-3, FR-PRS-2)
        Literal["pending", "confirmed", "rejected"]
    ]

    # ── Housekeeping ──────────────────────────────────────────────────────────
    errors: NotRequired[list[str]]
    current_node: NotRequired[str]

    # ── Streaming (injected by caller, not serialized) ────────────────────────
    _step_emitter: NotRequired[Any]
