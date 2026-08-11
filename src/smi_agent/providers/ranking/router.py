"""Two ranking arms, one entry point.

``rank_candidates`` is the single call site everything (itinerary graph,
Temporal activities, chat specialists) should use instead of calling
``annotate_reasons`` directly — it decides which arm serves a given user,
tags every candidate with the arm and the feature scores it was judged on
(so a later accept/reject event can be attributed and fed back to the
bandit), and falls back to the primitive arm whenever personalization isn't
possible (no user_id, no candidates, store unavailable).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal

from smi_agent.providers.ranking.features import extract_categorical, score_candidates
from smi_agent.providers.ranking.interface import RankingStore

logger = logging.getLogger(__name__)

Arm = Literal["primitive", "bandit"]


def select_arm(user_id: str, rollout_pct: float) -> Arm:
    """Deterministic per-user assignment — the same user always lands in the
    same arm for a given rollout_pct, so a session (and its downstream
    feedback) is never split across arms mid-flight. Not per-call random:
    that would make the primitive/bandit comparison meaningless.
    """
    if rollout_pct <= 0:
        return "primitive"
    if rollout_pct >= 100:
        return "bandit"
    bucket = int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 100
    return "bandit" if bucket < rollout_pct else "primitive"


async def rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    sort_by: str,
    price_field: str | None = None,
    rating_field: str | None = None,
    proximity_field: str | None = None,
    categorical_fields: dict[str, str] | None = None,
    user_id: str | None,
    store: RankingStore | None,
    rollout_pct: float = 0.0,
) -> tuple[list[dict[str, Any]], Arm]:
    """Rank+annotate candidates via whichever arm this user is assigned to.

    ``categorical_fields`` maps axis name -> candidate dict key, e.g.
    {"cuisine": "cuisine"} for restaurant search; omit for sections with no
    categorical axis (flight, hotel, attraction today).

    Returns (ranked_candidates, arm_used). Every returned candidate carries
    ``rank_arm``, ``rank_features`` (continuous axis scores), and
    ``rank_categorical`` (categorical axis -> tag) — feedback capture reads
    these back when the traveler later accepts/rejects/edits, so the bandit
    can learn regardless of which arm actually produced the recommendation.
    """
    from smi_agent.providers.explain import annotate_reasons, annotate_reasons_personalized

    if not candidates:
        return candidates, "primitive"

    feature_scores = score_candidates(
        candidates, price_field=price_field, rating_field=rating_field, proximity_field=proximity_field,
    )
    categorical_values = extract_categorical(candidates, field_map=categorical_fields or {})
    features_by_id = {c.get("id"): feature_scores[i] for i, c in enumerate(candidates)}
    categorical_by_id = {c.get("id"): categorical_values[i] for i, c in enumerate(candidates)}

    arm: Arm = select_arm(user_id, rollout_pct) if (user_id and store is not None) else "primitive"

    if arm == "bandit":
        try:
            weights = await store.get_weights(user_id)  # type: ignore[arg-type]
            ranked = annotate_reasons_personalized(
                candidates, weights=weights,
                price_field=price_field, rating_field=rating_field, proximity_field=proximity_field,
                categorical_fields=categorical_fields,
            )
        except Exception:
            logger.warning("bandit arm failed for user_id=%r — falling back to primitive", user_id, exc_info=True)
            arm = "primitive"
            ranked = annotate_reasons(
                candidates, sort_by=sort_by,
                price_field=price_field, rating_field=rating_field, proximity_field=proximity_field,
            )
    else:
        ranked = annotate_reasons(
            candidates, sort_by=sort_by,
            price_field=price_field, rating_field=rating_field, proximity_field=proximity_field,
        )

    return [
        {
            **c,
            "rank_arm": arm,
            "rank_features": features_by_id.get(c.get("id"), {}),
            "rank_categorical": categorical_by_id.get(c.get("id"), {}),
        }
        for c in ranked
    ], arm
