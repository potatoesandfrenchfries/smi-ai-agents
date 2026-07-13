"""Cache-aside helper backed by Redis.

Flight/hotel/restaurant searches are read far more often than the underlying
data changes, and each miss costs a real (rate-limited, sometimes flaky) HTTP
call to AviationStack/Overpass. get_or_set() wraps the "check cache, else
fetch and populate" dance once so the three scraper modules don't each
reimplement key hashing, TTLs, and (de)serialization.

If Redis is unreachable or the client isn't installed, every call degrades to
a live fetch instead of raising — caching is a performance optimization here,
never a hard dependency for search to work, mirroring the scrapers' own
fallback-to-mock-data behaviour when their upstream API is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None
_client_unavailable = False


def _get_client() -> Any | None:
    global _client, _client_unavailable
    if _client_unavailable:
        return None
    if _client is None:
        try:
            import redis.asyncio as aioredis

            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            _client = aioredis.from_url(redis_url, decode_responses=True)
        except ImportError:
            logger.warning("redis package not installed — search caching disabled")
            _client_unavailable = True
            return None
    return _client


def fingerprint(*parts: Any) -> str:
    """Stable short hash of the params that determine a fetch's result."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def get_or_set(
    key: str,
    ttl_seconds: int,
    fetch: Callable[[], Awaitable[Any]],
) -> Any:
    """Return the cached JSON-decoded value at `key`, else fetch, cache, and return it."""
    client = _get_client()

    if client is not None:
        try:
            cached = await client.get(key)
            if cached is not None:
                logger.debug("cache hit: %s", key)
                return json.loads(cached)
        except Exception as exc:
            logger.warning("Redis GET failed for %s (%s) — fetching live", key, exc)

    logger.debug("cache miss: %s", key)
    result = await fetch()

    if client is not None:
        try:
            await client.set(key, json.dumps(result, default=str), ex=ttl_seconds)
        except Exception as exc:
            logger.warning("Redis SET failed for %s (%s) — result not cached", key, exc)

    return result
