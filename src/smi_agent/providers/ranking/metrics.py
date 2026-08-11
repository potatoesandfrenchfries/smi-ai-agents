"""Aggregation over providers/ranking/'s event log.

Answers the third feedback-driven-improvement requirement directly: "track
whether recommendations become more relevant based on previous user
interactions." Reads straight from the event log rather than adding a new
persistence layer — RecommendationEvent already carries everything needed
(arm, action, per-user ordering via created_at).

Two different questions, two functions:
  - arm_summary: is the bandit arm doing better than the primitive baseline,
    overall? A between-group comparison — valid because select_arm assigns
    each user to one arm deterministically, not per-event at random.
  - relevance_trend: for the bandit arm specifically, does a user's Nth
    recommendation get accepted more often than their 1st? A within-arm,
    within-user comparison — the primitive arm has no learning mechanism,
    so a flat trend there is the expected baseline, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from smi_agent.providers.ranking.models import RecommendationEvent


@dataclass
class ArmSummary:
    arm: str
    total: int = 0
    accepted: int = 0
    rejected: int = 0

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total else 0.0


@dataclass
class RelevanceBucket:
    """Acceptance rate for the Nth..(N+bucket_size-1)th event in each user's
    own interaction history, pooled across every user. ``position_range`` is
    an index into one user's event sequence (0 = their first-ever event),
    not a calendar period — this is "does more accumulated feedback help",
    not "did this month do better than last month".
    """

    position_range: tuple[int, int]
    arm: str
    total: int = 0
    accepted: int = 0

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total else 0.0


def arm_summary(events: list[RecommendationEvent]) -> dict[str, ArmSummary]:
    """Overall accept-rate per arm."""
    summaries: dict[str, ArmSummary] = {}
    for event in events:
        s = summaries.setdefault(event.arm, ArmSummary(arm=event.arm))
        s.total += 1
        if event.action == "accepted":
            s.accepted += 1
        else:
            s.rejected += 1
    return summaries


def relevance_trend(
    events: list[RecommendationEvent], bucket_size: int = 5,
) -> list[RelevanceBucket]:
    """Does acceptance rate rise as a user accumulates more feedback events?

    Groups each user's events chronologically (position 0 = first event
    ever, by created_at), then pools all users' events by that position into
    buckets of ``bucket_size``, split by arm. A rising bandit-arm trend
    across buckets is the actual evidence the requirement asks for; the
    primitive arm is included as a control (it should stay roughly flat,
    since it never updates any weights).
    """
    by_user: dict[str, list[RecommendationEvent]] = {}
    for event in events:
        by_user.setdefault(event.user_id, []).append(event)

    buckets: dict[tuple[int, int, str], RelevanceBucket] = {}
    for user_events in by_user.values():
        user_events.sort(key=lambda e: e.created_at)
        for position, event in enumerate(user_events):
            lo = (position // bucket_size) * bucket_size
            hi = lo + bucket_size - 1
            key = (lo, hi, event.arm)
            b = buckets.setdefault(key, RelevanceBucket(position_range=(lo, hi), arm=event.arm))
            b.total += 1
            if event.action == "accepted":
                b.accepted += 1

    return sorted(buckets.values(), key=lambda b: (b.position_range, b.arm))


def summarize(events: list[RecommendationEvent], bucket_size: int = 5) -> dict:
    """Everything ranking_metrics_cli.py prints, bundled for convenience."""
    return {
        "total_events": len(events),
        "arm_summary": arm_summary(events),
        "relevance_trend": relevance_trend(events, bucket_size=bucket_size),
    }
