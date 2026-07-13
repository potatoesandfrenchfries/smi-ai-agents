"""File-backed TripStore.

One JSON file per trip, under ``<base_dir>/<user_id>/<trip_id>.json``. This
is a stand-in for a Postgres-backed TripStore — the directory-per-user
layout mirrors keying trips by user_id in a future ``trips`` table, so the
migration only means swapping this implementation, not the callers.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from smi_agent.trip_store.models import TripRecord

_DEFAULT_DIR = "data/trips"


class FileTripStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir or os.environ.get("SMI_TRIP_STORE_DIR", _DEFAULT_DIR))

    def _trip_path(self, user_id: str, trip_id: str) -> Path:
        return self._base_dir / user_id / f"{trip_id}.json"

    async def save_trip(self, record: TripRecord) -> None:
        await asyncio.to_thread(self._write, record)

    def _write(self, record: TripRecord) -> None:
        path = self._trip_path(record.user_id, record.trip_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(record), indent=2))

    async def get_trip(self, user_id: str, trip_id: str) -> TripRecord | None:
        return await asyncio.to_thread(self._read, user_id, trip_id)

    def _read(self, user_id: str, trip_id: str) -> TripRecord | None:
        path = self._trip_path(user_id, trip_id)
        if not path.exists():
            return None
        return TripRecord(**json.loads(path.read_text()))

    async def list_trips(self, user_id: str) -> list[TripRecord]:
        return await asyncio.to_thread(self._list, user_id)

    def _list(self, user_id: str) -> list[TripRecord]:
        user_dir = self._base_dir / user_id
        if not user_dir.exists():
            return []
        records = [TripRecord(**json.loads(p.read_text())) for p in user_dir.glob("*.json")]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records
