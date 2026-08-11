"""Per-candidate explainability — attaches a short, human-readable `reason`
to every search result so a traveler (or an upstream agent) can see *why*
an option ranks where it does, not just its price or rating.

The "best" candidate is found independently, by the given criterion field
(cheapest price / highest rating / closest proximity) — never by trusting
"index 0 = best". This matters because `reflect_itinerary` (the critic pass
in graph/itinerary_graph.py) can later reorder a section's candidate list to
promote a different one to the front, e.g. to work around a completeness
issue. If reasons were assigned by position, that reorder would silently
invalidate them (a cheaper option demoted to position 3 would still say
"cheapest of N", and whatever got promoted to position 0 would carry a
leftover "£X more than the top pick" reason). Computing "best" from each
candidate's own field values instead means every reason stays correct no
matter how the list is later reordered, with no re-annotation pass needed.
"""

from __future__ import annotations

from typing import Any


def annotate_reasons(
    candidates: list[dict[str, Any]],
    *,
    sort_by: str,
    price_field: str | None = None,
    rating_field: str | None = None,
    proximity_field: str | None = None,
) -> list[dict[str, Any]]:
    """Return a new list with a `reason` string added to every candidate.

    The best candidate (by `sort_by`'s criterion) is described in absolute
    terms (cheapest/highest-rated/closest of N); every other candidate is
    described relative to it, so a traveler sees not just what's best but
    why the rest rank below it.
    """
    if not candidates:
        return candidates

    n = len(candidates)
    best = _find_best(candidates, sort_by, price_field, rating_field, proximity_field)
    return [
        {**c, "reason": _reason(c, best, n, sort_by, price_field, rating_field, proximity_field)}
        for c in candidates
    ]


def annotate_reasons_personalized(
    candidates: list[dict[str, Any]],
    *,
    weights: Any,
    price_field: str | None = None,
    rating_field: str | None = None,
    proximity_field: str | None = None,
) -> list[dict[str, Any]]:
    """Like ``annotate_reasons``, but ranks by a per-user learned blend over
    normalized features (see providers/ranking/) instead of a single
    ``sort_by`` criterion — the personalized ranking arm.

    Unlike ``annotate_reasons``, which trusts the provider's own ordering
    and only annotates, this re-sorts by blended score descending: a
    personalized order has no other source of truth to defer to. ``weights``
    is a ``RankingWeights`` (or any object/mapping providing the same
    price/rating/proximity keys) — typed loosely here to avoid a module-level
    import cycle with providers/ranking.
    """
    from smi_agent.providers.ranking.features import blend, score_candidates

    if not candidates:
        return candidates

    n = len(candidates)
    feature_scores = score_candidates(
        candidates, price_field=price_field, rating_field=rating_field, proximity_field=proximity_field,
    )
    weights_dict = weights.as_dict() if hasattr(weights, "as_dict") else dict(weights)
    scored = sorted(
        zip(candidates, feature_scores, strict=True),
        key=lambda pair: blend(pair[1], weights_dict),
        reverse=True,
    )

    top_feature = max(weights_dict, key=weights_dict.get)
    return [
        {**c, "reason": _personalized_reason(i == 0, top_feature, n)}
        for i, (c, _feats) in enumerate(scored)
    ]


def _personalized_reason(is_best: bool, top_feature: str, n: int) -> str:
    if is_best:
        return f"Best match for your usual preferences, weighted most on {top_feature} (of {n} option(s) considered)"
    return f"Alternative option ranked by your usual preferences (of {n} option(s) considered)"


def _find_best(
    candidates: list[dict[str, Any]],
    sort_by: str,
    price_field: str | None,
    rating_field: str | None,
    proximity_field: str | None,
) -> dict[str, Any]:
    if sort_by in ("cost", "price") and price_field:
        return min(candidates, key=lambda c: c.get(price_field) if c.get(price_field) is not None else float("inf"))
    if sort_by in ("rating", "comfort") and rating_field:
        return max(candidates, key=lambda c: c.get(rating_field) or 0)
    if sort_by == "proximity" and proximity_field:
        return min(
            candidates,
            key=lambda c: c.get(proximity_field) if c.get(proximity_field) is not None else float("inf"),
        )
    # "time" / "match" / unrecognized sort_by have no independent field to
    # rank by here — trust the provider's own ordering for what counts as best.
    return candidates[0]


def _reason(
    c: dict[str, Any],
    best: dict[str, Any],
    n: int,
    sort_by: str,
    price_field: str | None,
    rating_field: str | None,
    proximity_field: str | None,
) -> str:
    if c is best:
        return _best_reason(c, n, sort_by, price_field, rating_field, proximity_field)
    return _alt_reason(c, best, n, price_field)


def _best_reason(
    c: dict[str, Any], n: int, sort_by: str,
    price_field: str | None, rating_field: str | None, proximity_field: str | None,
) -> str:
    if sort_by in ("cost", "price") and price_field and c.get(price_field) is not None:
        return f"Cheapest of {n} option(s) considered (£{c[price_field]:.2f})"
    if sort_by in ("rating", "comfort") and rating_field and c.get(rating_field) is not None:
        return f"Highest-rated of {n} option(s) considered ({c[rating_field]}/10)"
    if sort_by == "proximity" and proximity_field and c.get(proximity_field) is not None:
        return f"Closest of {n} option(s) considered ({c[proximity_field]} km from centre)"
    if sort_by == "time":
        return f"Fastest of {n} option(s) considered"
    if sort_by == "match":
        return f"Best cuisine match of {n} option(s) considered"
    return f"Top-ranked of {n} option(s) considered (by {sort_by})"


def _alt_reason(c: dict[str, Any], best: dict[str, Any], n: int, price_field: str | None) -> str:
    if price_field and c.get(price_field) is not None and best.get(price_field) is not None:
        delta = c[price_field] - best[price_field]
        if delta > 0.01:
            return f"£{delta:.2f} more than the best option (of {n} considered)"
    return f"Alternative option (of {n} considered)"
