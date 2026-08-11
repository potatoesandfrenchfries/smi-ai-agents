"""File-backed RankingStore.

One JSON file per user for weights, under ``<base_dir>/weights/<user_id>.json``,
and one append-only JSONL file per user for the event log, under
``<base_dir>/events/<user_id>.jsonl``. Mirrors trip_store/file_store.py's
directory-per-user layout — a stand-in for a Postgres-backed store; migrating
later means swapping this implementation, not the callers (routing layer,
record_ranking_feedback_activity).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from smi_agent.providers.ranking.models import RankingWeights, RecommendationEvent

_DEFAULT_DIR = "data/ranking"


class FileRankingStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir or os.environ.get("SMI_RANKING_STORE_DIR", _DEFAULT_DIR))

    def _weights_path(self, user_id: str) -> Path:
        return self._base_dir / "weights" / f"{user_id}.json"

    def _events_path(self, user_id: str) -> Path:
        return self._base_dir / "events" / f"{user_id}.jsonl"

    async def get_weights(self, user_id: str) -> RankingWeights:
        return await asyncio.to_thread(self._read_weights, user_id)

    def _read_weights(self, user_id: str) -> RankingWeights:
        path = self._weights_path(user_id)
        if not path.exists():
            return RankingWeights()
        return RankingWeights(**json.loads(path.read_text()))

    async def save_weights(self, user_id: str, weights: RankingWeights) -> None:
        await asyncio.to_thread(self._write_weights, user_id, weights)

    def _write_weights(self, user_id: str, weights: RankingWeights) -> None:
        path = self._weights_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(weights), indent=2))

    async def record_event(self, event: RecommendationEvent) -> None:
        await asyncio.to_thread(self._append_event, event)

    def _append_event(self, event: RecommendationEvent) -> None:
        path = self._events_path(event.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(asdict(event)) + "\n")

    async def list_events(self, user_id: str) -> list[RecommendationEvent]:
        """Not part of the RankingStore protocol — used by tests/inspection tools."""
        return await asyncio.to_thread(self._read_events, user_id)

    def _read_events(self, user_id: str) -> list[RecommendationEvent]:
        path = self._events_path(user_id)
        if not path.exists():
            return []
        return [
            RecommendationEvent(**json.loads(line))
            for line in path.read_text().splitlines()
            if line.strip()
        ]

    async def list_all_events(self) -> list[RecommendationEvent]:
        """Every event across every user — feeds providers/ranking/metrics.py.
        Not part of the RankingStore protocol (a Postgres-backed store would
        answer this with a plain SELECT, not a per-user file scan)."""
        return await asyncio.to_thread(self._read_all_events)

    def _read_all_events(self) -> list[RecommendationEvent]:
        events_dir = self._base_dir / "events"
        if not events_dir.exists():
            return []
        events: list[RecommendationEvent] = []
        for path in sorted(events_dir.glob("*.jsonl")):
            events.extend(
                RecommendationEvent(**json.loads(line))
                for line in path.read_text().splitlines()
                if line.strip()
            )
        return events
