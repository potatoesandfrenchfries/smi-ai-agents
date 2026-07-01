"""Langfuse client setup + LiteLLM callback integration.

Configures Langfuse tracing with session IDs keyed on ``investigation_id``.
LiteLLM's native Langfuse callback is used for per-LLM-call generation traces.
The ``configure_langfuse()`` function is called once at worker startup.

If ``LANGFUSE_SECRET_KEY`` is not set, all functions are no-ops (fail-safe).

Environment variables (all read from env by Langfuse SDK automatically):
    LANGFUSE_SECRET_KEY     Your Langfuse secret key
    LANGFUSE_PUBLIC_KEY     Your Langfuse public key
    LANGFUSE_HOST           API host (default: https://cloud.langfuse.com)

Usage::

    configure_langfuse()   # call once at startup

    # Inside a node:
    with investigation_trace(investigation_id, session_tag="uc02") as trace:
        trace.update(input={"exposure_id": "EXP-001"})
        ...
        trace.update(output={"status": "success"})
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_langfuse_enabled: bool = False
_langfuse_client: Any = None


def configure_langfuse() -> bool:
    """Initialise the Langfuse client if credentials are available.

    Returns:
        True if Langfuse is enabled, False if credentials are missing.
    """
    global _langfuse_enabled, _langfuse_client  # noqa: PLW0603

    if not os.environ.get("LANGFUSE_SECRET_KEY"):
        logger.debug("Langfuse disabled: LANGFUSE_SECRET_KEY not set")
        _langfuse_enabled = False
        return False

    try:
        from langfuse import Langfuse  # noqa: PLC0415

        _langfuse_client = Langfuse()
        _langfuse_enabled = True
        logger.info("Langfuse client initialised")

        # Register LiteLLM callback for per-call generation traces.
        import litellm  # noqa: PLC0415

        if "langfuse" not in litellm.success_callback:
            litellm.success_callback.append("langfuse")
        if "langfuse" not in litellm.failure_callback:
            litellm.failure_callback.append("langfuse")

        return True
    except ImportError:
        logger.warning(
            "langfuse package not installed — tracing disabled. Install it: pip install langfuse"
        )
        _langfuse_enabled = False
        return False


def is_enabled() -> bool:
    """Return True if Langfuse is configured and active."""
    return _langfuse_enabled


@contextmanager
def investigation_trace(
    investigation_id: str,
    exposure_id: str | None = None,
    session_tag: str = "default",
    **metadata: Any,
) -> Generator[Any, None, None]:
    """Context manager that creates a Langfuse trace for one investigation.

    Yields the trace object (or ``None`` if Langfuse is disabled).
    Marks the trace complete on exit.

    Usage::

        with investigation_trace("inv-001", exposure_id="EXP-1621") as trace:
            if trace:
                trace.update(metadata={"config": "uc02"})
            ...
    """
    if not _langfuse_enabled or _langfuse_client is None:
        yield None
        return

    trace = _langfuse_client.trace(
        id=investigation_id,
        name=f"investigate_exposure:{exposure_id or 'unknown'}",
        session_id=f"{session_tag}:{investigation_id}",
        metadata={
            "exposure_id": exposure_id,
            "session_tag": session_tag,
            **metadata,
        },
    )
    try:
        yield trace
    finally:
        try:
            _langfuse_client.flush()
        except Exception as exc:
            logger.debug("Langfuse flush failed: %s", exc)


def make_litellm_metadata(
    investigation_id: str,
    session_tag: str = "default",
    generation_name: str | None = None,
) -> dict[str, Any]:
    """Return a LiteLLM ``metadata`` dict that routes the call to Langfuse.

    Pass the returned dict as ``metadata=`` kwarg to ``litellm.acompletion``.
    If Langfuse is disabled, returns an empty dict.
    """
    if not _langfuse_enabled:
        return {}
    meta: dict[str, Any] = {
        "trace_id": investigation_id,
        "session_id": f"{session_tag}:{investigation_id}",
    }
    if generation_name:
        meta["generation_name"] = generation_name
    return meta
