"""Token estimation for conversation history."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def count_message_tokens(messages: list[dict[str, Any]], model: str = "claude-3-haiku") -> int:
    """Estimate token count for a list of message dicts (synchronous, may be CPU-bound)."""
    if not messages:
        return 0
    try:
        import tiktoken  # noqa: PLC0415

        enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(enc.encode(content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(enc.encode(block.get("text", "")))
        return total
    except Exception as exc:
        logger.debug("count_message_tokens: tiktoken fallback due to %s", type(exc).__name__)
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
        return total


async def count_message_tokens_async(
    messages: list[dict[str, Any]], model: str = "claude-3-haiku"
) -> int:
    """Async wrapper — offloads tiktoken CPU work to a thread pool."""
    return await asyncio.to_thread(count_message_tokens, messages, model)


def should_compact(
    history_tokens: int,
    context_window_tokens: int,
    threshold_pct: float,
) -> bool:
    if context_window_tokens <= 0:
        return False
    return (history_tokens / context_window_tokens) * 100 >= threshold_pct
