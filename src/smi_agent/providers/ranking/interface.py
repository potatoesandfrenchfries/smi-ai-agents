"""Storage interface for personalized ranking.

FileRankingStore is the only implementation today. A future
PostgresRankingStore can implement this same Protocol so callers (the
routing layer, the feedback activity) never need to change when the backend
does — same pattern as trip_store/interface.py::TripStore.
"""

from __future__ import annotations

from typing import Protocol

from smi_agent.providers.ranking.models import RankingWeights, RecommendationEvent


class RankingStore(Protocol):
    async def get_weights(self, user_id: str) -> RankingWeights: ...

    async def save_weights(self, user_id: str, weights: RankingWeights) -> None: ...

    async def record_event(self, event: RecommendationEvent) -> None: ...
