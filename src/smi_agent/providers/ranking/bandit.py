"""Online weight update for the personalized ranking arm.

A multiplicative-weights update (Hedge/Exp3-style) rather than full RL: each
accept/reject event nudges the weight on every feature in proportion to how
much that candidate scored on it, then renormalizes. Bounded, stable, no
gradient/learning-rate schedule to tune, and converges from very few events —
the right-sized technique for a single-shot ranking decision with no
sequential/delayed-reward structure (see conversation: full RL would need
much more interaction data than exists here to be worth the instability).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from smi_agent.providers.ranking.models import FEATURE_NAMES, RankingWeights

# Conservative on purpose: with few events per user early on, a large update
# would let a single accept/reject swing the ranking too far. 0.15 means even
# a maximally-informative event (feature score of 1.0) moves that feature's
# raw weight by ~16% before renormalization.
_LEARNING_RATE = 0.15

# Floor so no feature's weight can be driven to (near) zero — a
# multiplicative update can never recover a weight that hits zero, since
# every subsequent update multiplies it by something and 0 * anything = 0.
_MIN_WEIGHT = 0.01


def update_weights(
    current: RankingWeights, features: dict[str, float], reward: float,
) -> RankingWeights:
    """Return new weights after one accept (reward=+1.0) or reject (reward=-1.0) event.

    ``features`` are the candidate's normalized 0..1 scores (see
    features.py::score_candidates) at the moment it was shown — a feature the
    candidate scored high on gets more weight on accept, less on reject.
    """
    raw = {
        name: max(_MIN_WEIGHT, getattr(current, name) * math.exp(_LEARNING_RATE * reward * features.get(name, 0.5)))
        for name in FEATURE_NAMES
    }
    total = sum(raw.values())
    normalized = {name: v / total for name, v in raw.items()}

    return RankingWeights(
        **normalized,
        event_count=current.event_count + 1,
        updated_at=datetime.now(UTC).isoformat(),
    )
