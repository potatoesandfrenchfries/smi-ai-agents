"""Online weight updates for the personalized ranking arm.

A multiplicative-weights update (Hedge/Exp3-style) rather than full RL: each
accept/reject event nudges weights in proportion to how much a candidate
scored on each axis, then renormalizes. Bounded, stable, no gradient/
learning-rate schedule to tune, and converges from very few events — the
right-sized technique for a single-shot ranking decision with no
sequential/delayed-reward structure.

Two update rules, matching the two-level RankingWeights structure:
  - update_axis_weights: dense, every axis moves a little on every event —
    "how much does price/rating/proximity/cuisine matter overall".
  - update_tag_weight: sparse, only the one tag the reacted-to candidate
    actually had moves — "given cuisine matters, which cuisine". Every other
    tag in that axis is untouched except for the renormalization that
    naturally follows from one tag going up (or down).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime

from smi_agent.providers.ranking.models import RankingWeights

# Conservative on purpose: with few events per user early on, a large update
# would let a single accept/reject swing the ranking too far. 0.15 means even
# a maximally-informative event (feature/tag score of 1.0) moves that axis's
# raw weight by ~16% before renormalization.
_LEARNING_RATE = 0.15

# Floor so no axis/tag's weight can be driven to (near) zero — a
# multiplicative update can never recover a weight that hits zero, since
# every subsequent update multiplies it by something and 0 * anything = 0.
_MIN_WEIGHT = 0.01


def reward_from_rating(rating: int) -> float:
    """Map a 1-5 star rating onto the same -1..+1 reward scale plain
    accept/reject events use: 1->-1.0, 2->-0.5, 3->0.0, 4->+0.5, 5->+1.0.

    A rating of exactly 3 contributes zero reward — genuinely neutral,
    neither reinforcing nor discouraging the axes/tags involved — rather
    than being forced into "accepted" or "rejected" the way the coarse
    action field has to be. Clamped to 1..5 in case a caller passes
    something out of range.
    """
    rating = max(1, min(5, rating))
    return (rating - 3) / 2.0


def update_axis_weights(
    current: RankingWeights, axis_scores: dict[str, float], reward: float,
) -> RankingWeights:
    """Return new weights after one accept (reward=+1.0) or reject (reward=-1.0) event.

    ``axis_scores`` are each axis's 0..1 score for the candidate this event
    is about — continuous axes' normalized feature score (see
    features.py::score_candidates), categorical axes' current tag weight
    (see features.py::blend) — keyed the same as ``current.axis_weights``.
    An axis missing from ``axis_scores`` is treated as neutral (0.5), same
    as a candidate with no value for that field.
    """
    raw = {
        axis: max(_MIN_WEIGHT, w * math.exp(_LEARNING_RATE * reward * axis_scores.get(axis, 0.5)))
        for axis, w in current.axis_weights.items()
    }
    total = sum(raw.values())
    new_axis_weights = {axis: v / total for axis, v in raw.items()}

    return replace(
        current,
        axis_weights=new_axis_weights,
        event_count=current.event_count + 1,
        updated_at=datetime.now(UTC).isoformat(),
    )


def categorical_axis_score(current: RankingWeights, axis: str, tag: str | None) -> float:
    """The axis-level score for a categorical axis, fed into
    update_axis_weights alongside continuous feature scores — "how much did
    we already believe in this tag" (current weight within the axis), or
    the axis's average tag weight if the tag is missing/unrecognized. Same
    fallback rule as features.py::blend, kept here rather than imported
    since this is a feedback-time concern, not a ranking-time one.
    """
    tag_dist = current.tag_weights.get(axis)
    if not tag_dist:
        return 0.5
    if tag and tag in tag_dist:
        return tag_dist[tag]
    return sum(tag_dist.values()) / len(tag_dist)


def update_tag_weight(
    current: RankingWeights, axis: str, tag: str, reward: float,
) -> RankingWeights:
    """Sparse update: bump ``tag``'s weight within ``axis``'s distribution,
    renormalize only that axis's tags. No-ops if the axis or tag isn't in
    the user's known tag set — an unrecognized tag has nothing to learn
    from safely (see conversation: new tags need an explicit onboarding
    policy, not a silent insert here).
    """
    tag_dist = current.tag_weights.get(axis)
    if tag_dist is None or tag not in tag_dist:
        return current

    raw = dict(tag_dist)
    raw[tag] = max(_MIN_WEIGHT, raw[tag] * math.exp(_LEARNING_RATE * reward))
    total = sum(raw.values())
    normalized = {t: v / total for t, v in raw.items()}

    new_tag_weights = dict(current.tag_weights)
    new_tag_weights[axis] = normalized

    return replace(current, tag_weights=new_tag_weights, updated_at=datetime.now(UTC).isoformat())
