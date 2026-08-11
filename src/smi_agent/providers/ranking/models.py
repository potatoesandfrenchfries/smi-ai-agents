"""Shapes for the personalized-ranking subsystem.

Two kinds of ranking axis, one weight structure:
  - Continuous (price/rating/proximity) — peer-normalized 0..1 scores,
    see features.py::score_candidates.
  - Categorical (cuisine, ...) — a fixed tag set per axis; a candidate has
    exactly one tag per axis, see features.py::extract_categorical.

``axis_weights`` says how much each axis (continuous or categorical) counts
in the overall blend, and sums to 1.0 across *every* axis together.
``tag_weights`` is only for categorical axes: within one axis, "given that
cuisine matters, which cuisine" — each axis's tag distribution sums to 1.0
*on its own*, independent of the others. Keeping these separate is what
lets a user who doesn't care about cuisine at all look different from one
who cares a lot but has no strong single-cuisine preference (see
conversation: conflating them into one flat vector loses that distinction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Peer-normalized continuous features — single source of truth for
# features.py, bandit.py, and explain.py.
CONTINUOUS_FEATURE_NAMES: tuple[str, ...] = ("price", "rating", "proximity")

# Categorical axes and their fixed tag sets. Extend this dict to add a new
# categorical axis (transport mode, lodging type, ...) — everything else
# (features.py, bandit.py, explain.py) reads tags from RankingWeights.tag_weights,
# not from this constant directly, so adding an axis here only changes what
# a *new* RankingWeights defaults to.
CATEGORICAL_AXES: dict[str, tuple[str, ...]] = {
    "cuisine": ("italian", "indian", "french", "continental", "american"),
    # Real category values (mock and live OSM) are more granular than this —
    # examples/travel/tools/attraction_scraper.py maps them down to this
    # coarse set before ranking sees them, same reasoning as cuisine: a
    # smaller tag set converges from fewer events, at the cost of nuance.
    "attraction_type": ("monument", "park", "museum", "entertainment"),
    # Matches examples/travel/tools/hotel_scraper.py's OSM tourism-tag filter
    # exactly (see _OSM_TOURISM_VALUES there) — no "villa"/"apartment" tag,
    # since neither the live query nor the mock data can back one yet.
    "lodging_type": ("hotel", "guest_house", "hostel", "motel"),
}


def _default_axis_weights() -> dict[str, float]:
    axes = [*CONTINUOUS_FEATURE_NAMES, *CATEGORICAL_AXES]
    return dict.fromkeys(axes, 1.0 / len(axes))


def _default_tag_weights() -> dict[str, dict[str, float]]:
    return {
        axis: dict.fromkeys(tags, 1.0 / len(tags))
        for axis, tags in CATEGORICAL_AXES.items()
    }


@dataclass
class RankingWeights:
    """A user's learned blend over ranking axes, continuous and categorical."""

    axis_weights: dict[str, float] = field(default_factory=_default_axis_weights)
    tag_weights: dict[str, dict[str, float]] = field(default_factory=_default_tag_weights)
    event_count: int = 0  # how many feedback events have shaped these weights
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


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
    features: dict[str, float] = field(default_factory=dict)  # continuous axis scores at decision time
    categorical: dict[str, str] = field(default_factory=dict)  # categorical axis -> tag at decision time
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
