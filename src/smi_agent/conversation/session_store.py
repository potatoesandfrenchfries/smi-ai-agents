"""Redis-backed conversation session store."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from smi_agent.config.redis_keys import session_history_key, session_meta_key
from smi_agent.conversation.schemas import SessionMeta

logger = logging.getLogger(__name__)


@runtime_checkable
class SessionStore(Protocol):
    async def load(self, session_id: str) -> tuple[SessionMeta, list[dict[str, Any]]] | None: ...
    async def save(self, meta: SessionMeta, history: list[dict[str, Any]]) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def touch(self, session_id: str) -> None: ...


class RedisSessionStore:
    def __init__(self, redis_client: Any, ttl_seconds: int = 604800) -> None:
        self._r = redis_client
        self._ttl = ttl_seconds

    async def load(self, session_id: str) -> tuple[SessionMeta, list[dict[str, Any]]] | None:
        meta_key = session_meta_key(session_id)
        hist_key = session_history_key(session_id)
        meta_raw, hist_raw = await self._r.mget(meta_key, hist_key)
        if meta_raw is None:
            return None
        meta = SessionMeta.model_validate_json(meta_raw)
        history: list[dict[str, Any]] = json.loads(hist_raw) if hist_raw else []
        return meta, history

    async def save(self, meta: SessionMeta, history: list[dict[str, Any]]) -> None:
        sid = meta.session_id
        meta_key = session_meta_key(sid)
        hist_key = session_history_key(sid)
        pipe = self._r.pipeline()
        pipe.set(meta_key, meta.model_dump_json())
        pipe.set(hist_key, json.dumps(history, default=str))
        pipe.expire(meta_key, self._ttl)
        pipe.expire(hist_key, self._ttl)
        await pipe.execute()

    async def delete(self, session_id: str) -> None:
        meta_key = session_meta_key(session_id)
        hist_key = session_history_key(session_id)
        await self._r.delete(meta_key, hist_key)

    async def touch(self, session_id: str) -> None:
        meta_key = session_meta_key(session_id)
        hist_key = session_history_key(session_id)
        await self._r.expire(meta_key, self._ttl)
        await self._r.expire(hist_key, self._ttl)


def make_redis_session_store(url: str, ttl_seconds: int = 604800) -> RedisSessionStore:
    """Create a RedisSessionStore. Raises ImportError with install hint if redis not available."""
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "redis package is required for session storage. Install with: uv add 'redis[asyncio]'"
        ) from exc
    client = aioredis.from_url(url, decode_responses=False)
    return RedisSessionStore(client, ttl_seconds=ttl_seconds)
