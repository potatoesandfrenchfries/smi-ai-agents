"""Shape of a confirmed trip, as persisted for later lookup across conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TripRecord:
    trip_id: str
    user_id: str
    tenant_id: str
    status: str
    origin: str
    destination: str
    check_in: str
    check_out: str
    segments: list[dict] = field(default_factory=list)
    dining_options: list[dict] = field(default_factory=list)
    total_cost_gbp: float | None = None
    policy_status: str = "pending"
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_alternatives: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
