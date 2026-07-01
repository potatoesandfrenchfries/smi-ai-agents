"""Redis pub/sub SSE fan-out for multi-instance streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from smi_agent.config.redis_keys import sse_channel

logger = logging.getLogger(__name__)


class SSEStreamer:
    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client

    def _channel(self, session_id: str) -> str:
        return sse_channel(session_id)

    async def publish_token(self, session_id: str, token: str) -> None:
        payload = json.dumps({"type": "token", "data": token})
        await self._r.publish(self._channel(session_id), payload)

    async def publish_done(self, session_id: str) -> None:
        payload = json.dumps({"type": "done"})
        await self._r.publish(self._channel(session_id), payload)

    async def publish_error(self, session_id: str, error: str) -> None:
        payload = json.dumps({"type": "error", "data": error})
        await self._r.publish(self._channel(session_id), payload)

    async def publish_step(self, session_id: str, step_data: dict) -> None:
        """Publish a reasoning step event."""
        payload = json.dumps({"type": "step", "data": step_data})
        await self._r.publish(self._channel(session_id), payload)

    async def publish_response(self, session_id: str, response_data: dict) -> None:
        """Publish a structured response event (multi-agent chat)."""
        payload = json.dumps({"type": "response", "data": response_data})
        await self._r.publish(self._channel(session_id), payload)

    async def open_channel(self, session_id: str) -> Any:
        """Subscribe eagerly and return the pubsub handle.

        Call this BEFORE scheduling the background task that will publish, to
        eliminate the race condition where publish_done fires before subscribe.
        """
        pubsub = self._r.pubsub()
        await pubsub.subscribe(self._channel(session_id))
        return pubsub

    async def stream_channel(self, pubsub: Any, session_id: str) -> AsyncIterator[str]:
        """Stream SSE events from an already-subscribed pubsub handle.

        If Redis times out (background task crashed or took too long),
        emits an error + done event to the client instead of silently dying.
        """
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = json.loads(message["data"])
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("type") == "done":
                    break
        except Exception as exc:
            logger.warning("SSE stream_channel error for %s: %s", session_id, exc)
            error_payload = {"type": "error", "data": f"Stream interrupted: {type(exc).__name__}"}
            yield f"data: {json.dumps(error_payload)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            try:
                await pubsub.unsubscribe(self._channel(session_id))
                await pubsub.aclose()
            except Exception:
                pass  # Best-effort cleanup
