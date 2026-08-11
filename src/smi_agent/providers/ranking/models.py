"""Shapes for the personalized-ranking subsystem.

RankingWeights is a per-user blend over normalized candidate features
(price/rating/proximity — see ``features.py``), updated online from
RecommendationEvents. Both are plain dataclasses, same as
``trip_store/models.py``, so they serialize trivially to JSON for the
file-backed store today and to Postgres rows later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Every normalized feature the bandit can weight. Kept as a single source of
# truth so features.py, bandit.py, and the store all agree on the key set.
FEATURE_NAMES: tuple[str, ...] = ("price", "rating", "proximity")


@dataclass
class RankingWeights:
    """A user's learned blend over ranking features. Always sums to 1.0."""

    price: float = 1.0 / 3
    rating: float = 1.0 / 3
    proximity: float = 1.0 / 3
    event_count: int = 0  # how many feedback events have shaped this weight vector
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in FEATURE_NAMES}


@dataclass
class RecommendationEvent:
    """One accept/reject signal tied to a specific candidate and the arm
    (primitive/bandit) that produced it — the reward the bandit learns from.
    """

    event_id: str
    user_id: str
    tenant_id: str
    section: str  # "flight" | "hotel" | "restaurant" | "attraction"
    candidate_id: str
    action: str  # "accepted" | "rejected"
    arm: str  # "primitive" | "bandit"
    features: dict[str, float] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
