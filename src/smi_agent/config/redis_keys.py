"""Centralized Redis key prefixes — environment-aware.

All Redis keys in the application go through this module so that dev/staging
keys are isolated from production keys on a shared Redis instance.

Environment variable:
    SMI_ENV    dev | staging | prod  (default: prod)

Key layout:
    prod:       smi:session:{sid}:meta
    dev:        dev:smi:session:{sid}:meta
    staging:    staging:smi:session:{sid}:meta
"""

from __future__ import annotations

import os

_ALLOWED_ENVS = frozenset({"prod", "dev", "staging", "local", "test"})

_ENV = os.environ.get("SMI_ENV", "prod").lower().strip()
if _ENV not in _ALLOWED_ENVS:
    raise ValueError(f"Invalid SMI_ENV={_ENV!r}. Allowed: {sorted(_ALLOWED_ENVS)}")

# Production has no prefix; all others get "{env}:" prepended
_PREFIX = "" if _ENV == "prod" else f"{_ENV}:"


def session_meta_key(session_id: str) -> str:
    """Redis key for session metadata."""
    return f"{_PREFIX}smi:session:{session_id}:meta"


def session_history_key(session_id: str) -> str:
    """Redis key for session message history."""
    return f"{_PREFIX}smi:session:{session_id}:history"


def sse_channel(channel_id: str) -> str:
    """Redis pub/sub channel for SSE streaming."""
    return f"{_PREFIX}smi:sse:{channel_id}"


def user_context_key(user_id: str) -> str:
    """Redis key for cached user context (from auth headers)."""
    return f"{_PREFIX}smi:user:{user_id}"


def flight_cache_key(fingerprint: str) -> str:
    """Redis key for a cached flight search result."""
    return f"{_PREFIX}smi:cache:flight:{fingerprint}"


def hotel_cache_key(fingerprint: str) -> str:
    """Redis key for a cached hotel search result."""
    return f"{_PREFIX}smi:cache:hotel:{fingerprint}"


def restaurant_cache_key(fingerprint: str) -> str:
    """Redis key for a cached restaurant search result."""
    return f"{_PREFIX}smi:cache:restaurant:{fingerprint}"


def get_env() -> str:
    """Return the current environment name."""
    return _ENV