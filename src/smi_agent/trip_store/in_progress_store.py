"""File-backed index of unconfirmed, still-open itinerary plans.

Confirmed trips live in FileTripStore once a workflow completes. Before
that, a plan only exists as a running Temporal workflow with no durable
record a fresh CLI invocation can look up. This is that record — just
enough to reconnect a workflow handle by id so a later "modify my trip"
message doesn't need a fresh generate_itinerary run.

One JSON file per plan, under <base_dir>/<user_id>/<plan_id>.json — same
layout as FileTripStore, deliberately under a separate base_dir so an
in-progress plan and a later confirmed trip with the same ids never collide.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_DIR = "data/in_progress_plans"


@dataclass
class InProgressPlan:
    plan_id: str
    workflow_id: str
    user_id: str
    tenant_id: str
    raw_goal: str
    origin: str
    destination: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class InProgressPlanStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir or os.environ.get("SMI_IN_PROGRESS_DIR", _DEFAULT_DIR))

    def _plan_path(self, user_id: str, plan_id: str) -> Path:
        return self._base_dir / user_id / f"{plan_id}.json"

    async def save(self, record: InProgressPlan) -> None:
        await asyncio.to_thread(self._write, record)

    def _write(self, record: InProgressPlan) -> None:
        path = self._plan_path(record.user_id, record.plan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(record), indent=2))

    async def list_for_user(self, user_id: str) -> list[InProgressPlan]:
        return await asyncio.to_thread(self._list, user_id)

    def _list(self, user_id: str) -> list[InProgressPlan]:
        user_dir = self._base_dir / user_id
        if not user_dir.exists():
            return []
        records = [InProgressPlan(**json.loads(p.read_text())) for p in user_dir.glob("*.json")]
        records.sort(key=lambda r: r.started_at, reverse=True)
        return records

    async def delete(self, user_id: str, plan_id: str) -> None:
        await asyncio.to_thread(self._delete, user_id, plan_id)

    def _delete(self, user_id: str, plan_id: str) -> None:
        self._plan_path(user_id, plan_id).unlink(missing_ok=True)
