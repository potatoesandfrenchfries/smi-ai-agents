"""StepEmitter — publishes reasoning steps to Redis pub/sub for SSE delivery.

Each step is a structured event that the frontend renders as a progress line
(with status icon, message, and optional detail).  Steps share the same Redis
channel as conversation token/error/done events (``smi:sse:{channel_id}``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from smi_agent.config.redis_keys import sse_channel as _sse_channel

logger = logging.getLogger(__name__)

StepStatus = Literal["pending", "in_progress", "completed", "failed"]


class StepEmitter:
    """Publishes ``{"type": "step", ...}`` events to a Redis pub/sub channel."""

    def __init__(self, redis_client: Any, channel_id: str) -> None:
        self._r = redis_client
        self._channel = _sse_channel(channel_id)
        self._seq = 0

    async def emit(
        self,
        node: str,
        status: StepStatus,
        message: str,
        *,
        step_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Publish a reasoning step event.

        Args:
            node: Graph node name that emitted this step.
            status: One of ``pending``, ``in_progress``, ``completed``, ``failed``.
            message: Human-readable progress message.
            step_id: Optional unique id for idempotent frontend updates.
                     Auto-generated as ``{node}-{seq:03d}`` if omitted.
            detail: Optional structured metadata (must be JSON-serialisable).
        """
        self._seq += 1
        payload = {
            "type": "step",
            "data": {
                "step_id": step_id or f"{node}-{self._seq:03d}",
                "node": node,
                "status": status,
                "message": message,
                "detail": detail,
                "ts": datetime.now(UTC).isoformat(),
                "seq": self._seq,
            },
        }
        try:
            await self._r.publish(self._channel, json.dumps(payload))
        except Exception:
            logger.warning(
                "StepEmitter: failed to publish step (node=%s seq=%d)",
                node,
                self._seq,
                exc_info=True,
            )


class NullStepEmitter(StepEmitter):
    """No-op emitter for tests and environments without Redis."""

    def __init__(self) -> None:  # noqa: D107
        self._seq = 0

    async def emit(
        self,
        node: str,
        status: StepStatus,
        message: str,
        *,
        step_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Silently discard the step."""


def get_step_emitter(config: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> StepEmitter:
    """Extract a ``StepEmitter`` from state or config.

    Priority: state._step_emitter > config.metadata > config.configurable.
    LangGraph 0.6+ strips unknown keys from config, so state is the
    reliable path for passing the emitter from the runner.

    Returns ``NullStepEmitter`` when no emitter is found.
    """
    # 1. State (injected by conversation_runner / investigation_runner)
    if state and state.get("_step_emitter"):
        return state["_step_emitter"]
    # 2. Config metadata
    if config:
        emitter = config.get("metadata", {}).get("step_emitter")
        if emitter:
            return emitter
        # 3. Config configurable (legacy / pre-0.6)
        emitter = config.get("configurable", {}).get("step_emitter")
        if emitter:
            return emitter
    return NullStepEmitter()
