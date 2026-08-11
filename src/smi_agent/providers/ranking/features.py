"""Normalize raw candidate fields into comparable 0..1 scores, higher-is-better.

Shared by the bandit ranking arm (blends these via RankingWeights) and
feedback capture (snapshots a candidate's scores at decision time so a later
accept/reject event can be attributed back to specific features). Peer-
normalized against the candidate set in front of it, same relative-comparison
principle providers/explain.py already uses — not an absolute scale, so a
"good" price score means "cheap relative to its peers in this search", not
cheap in some global sense.
"""

from __future__ import annotations

from typing import Any

from smi_agent.providers.ranking.models import FEATURE_NAMES

_NEUTRAL = 0.5


def score_candidates(
    candidates: list[dict[str, Any]],
    *,
    price_field: str | None = None,
    rating_field: str | None = None,
    proximity_field: str | None = None,
) -> list[dict[str, float]]:
    """Return one feature-score dict per candidate, same order as input.

    Every dict has all of FEATURE_NAMES — an axis with no applicable field
    for this section (e.g. no rating_field for flights) gets the neutral
    0.5 for every candidate, so it doesn't skew a blend that weights it.
    """
    price_scores = _normalize(candidates, price_field, higher_is_better=False)
    rating_scores = _normalize(candidates, rating_field, higher_is_better=True)
    proximity_scores = _normalize(candidates, proximity_field, higher_is_better=False)

    return [
        {"price": price_scores[i], "rating": rating_scores[i], "proximity": proximity_scores[i]}
        for i in range(len(candidates))
    ]


def _normalize(
    candidates: list[dict[str, Any]], field_name: str | None, *, higher_is_better: bool,
) -> list[float]:
    if not field_name:
        return [_NEUTRAL] * len(candidates)

    values: list[float | None] = [c.get(field_name) for c in candidates]
    present = [v for v in values if v is not None]
    if not present:
        return [_NEUTRAL] * len(candidates)

    lo, hi = min(present), max(present)
    if hi - lo < 1e-9:
        # No discriminating signal (all equal, or only one candidate) — every
        # candidate is equally good/bad on this axis, so neutral rather than
        # an arbitrary 1.0 for all of them.
        return [_NEUTRAL] * len(candidates)

    def _score(v: float | None) -> float:
        if v is None:
            return _NEUTRAL
        frac = (v - lo) / (hi - lo)
        return frac if higher_is_better else 1.0 - frac

    return [_score(v) for v in values]


def blend(features: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of a candidate's feature scores — the bandit's ranking score."""
    return sum(weights.get(name, 0.0) * features.get(name, _NEUTRAL) for name in FEATURE_NAMES)
