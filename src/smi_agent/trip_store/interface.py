"""Storage interface for confirmed trips.

FileTripStore is the only implementation today. A future PostgresTripStore
can implement this same Protocol so callers (the workflow activity, the
lookup CLI) never need to change when the backend does.
"""

from __future__ import annotations

from typing import Protocol

from smi_agent.trip_store.models import TripRecord


class TripStore(Protocol):
    async def save_trip(self, record: TripRecord) -> None: ...

    async def get_trip(self, user_id: str, trip_id: str) -> TripRecord | None: ...

    async def list_trips(self, user_id: str) -> list[TripRecord]: ...
