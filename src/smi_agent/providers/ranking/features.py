"""Score candidates on each ranking axis, continuous and categorical.

Continuous axes (price/rating/proximity) are peer-normalized 0..1 scores,
higher-is-better, relative to the candidate set in front of them — not an
absolute scale. Categorical axes (cuisine, ...) have no such ordering: a
candidate simply has one tag or it doesn't, and "how good" that tag is comes
entirely from the user's learned tag_weights, not from the candidates here.

Shared by the bandit ranking arm (blends these via RankingWeights) and
feedback capture (snapshots a candidate's scores/tag at decision time so a
later accept/reject event can be attributed back to the right axis and tag).
"""

from __future__ import annotations

from typing import Any

from smi_agent.providers.ranking.models import CONTINUOUS_FEATURE_NAMES, RankingWeights

_NEUTRAL = 0.5


def score_candidates(
    candidates: list[dict[str, Any]],
    *,
    price_field: str | None = None,
    rating_field: str | None = None,
    proximity_field: str | None = None,
) -> list[dict[str, float]]:
    """Return one continuous feature-score dict per candidate, same order as input.

    Every dict has all of CONTINUOUS_FEATURE_NAMES — an axis with no
    applicable field for this section (e.g. no rating_field for flights)
    gets the neutral 0.5 for every candidate, so it doesn't skew a blend
    that weights it.
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


def extract_categorical(
    candidates: list[dict[str, Any]], *, field_map: dict[str, str],
) -> list[dict[str, str | None]]:
    """Return one {axis: tag} dict per candidate, same order as input.

    ``field_map`` maps axis name -> the key holding that value on the raw
    candidate dict, e.g. {"cuisine": "cuisine"}. A candidate's raw value is
    matched case-insensitively against RankingWeights.tag_weights' known tag
    set at blend time — this function just extracts and normalizes casing,
    it doesn't know which tags are "recognized" (that's a per-user concern,
    since tag sets live on RankingWeights, not here).
    """
    if not field_map:
        return [{} for _ in candidates]
    return [
        {axis: _normalize_tag(c.get(key)) for axis, key in field_map.items()}
        for c in candidates
    ]


def _normalize_tag(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower()


def blend(
    continuous: dict[str, float], categorical: dict[str, str | None], weights: RankingWeights,
) -> float:
    """Weighted sum of a candidate's axis scores — the bandit's ranking score.

    Continuous axes contribute their normalized score directly. Categorical
    axes contribute the weight of the candidate's specific tag within that
    axis's distribution — or, if the candidate's tag isn't in the user's
    known tag set (unrecognized cuisine, missing field, ...), the average
    weight across that axis's tags, so an unrecognized tag neither helps nor
    hurts relative to one the user has never expressed an opinion on.
    """
    score = 0.0
    for axis, axis_weight in weights.axis_weights.items():
        if axis in CONTINUOUS_FEATURE_NAMES:
            score += axis_weight * continuous.get(axis, _NEUTRAL)
        else:
            tag_dist = weights.tag_weights.get(axis)
            if not tag_dist:
                continue
            tag = categorical.get(axis)
            tag_score = tag_dist.get(tag) if tag else None
            if tag_score is None:
                tag_score = sum(tag_dist.values()) / len(tag_dist)
            score += axis_weight * tag_score
    return score
