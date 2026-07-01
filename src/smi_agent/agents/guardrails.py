"""Input/output guardrails for the multi-agent chat system.

Input: message length, prompt injection detection.
Output: strip raw queries, PII redaction, max blocks, entity ID validation.
"""

from __future__ import annotations

import json
import logging
import re

from smi_agent.agents.response import ContentBlock, StructuredResponse

logger = logging.getLogger(__name__)

# ── Input Guardrails ──────────────────────────────────────────────────────────

_INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|above|prior|earlier)\s+(instructions|prompts|rules|context)",
    r"you\s+are\s+now\s+a",
    r"disregard\s+(everything|all|your)",
    r"system\s+prompt",
    r"jailbreak",
    r"DAN\s+mode",
    r"forget\s+(your|all|the)\s+(rules|instructions|guidelines)",
    r"new\s+instructions?\s*:",
    r"override\s+(your|all|the)",
    r"act\s+as\s+(if|though)\s+you",
    r"pretend\s+(you|to\s+be)",
    r"reveal\s+(your|the)\s+(system|prompt|instructions)",
    r"what\s+(are|is)\s+your\s+(system|initial)\s+(prompt|instructions)",
    r"repeat\s+(your|the)\s+(system|initial)\s+(prompt|message)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def _normalize_text(text: str) -> str:
    """Normalize Unicode to catch homoglyph/zero-width bypasses."""
    import unicodedata
    # NFKC normalization: converts homoglyphs, removes zero-width chars
    normalized = unicodedata.normalize("NFKC", text)
    # Strip zero-width characters that survive NFKC
    normalized = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", normalized)
    return normalized

MAX_MESSAGE_LENGTH = 4096


class InputGuardrails:
    """Validates user input before processing."""

    @staticmethod
    def validate(message: str) -> tuple[bool, str | None]:
        """Validate user message. Returns (is_valid, error_reason)."""
        if not message or not message.strip():
            return False, "Message cannot be empty"

        if len(message) > MAX_MESSAGE_LENGTH:
            return False, f"Message too long ({len(message)} chars, max {MAX_MESSAGE_LENGTH})"

        # Normalize to catch Unicode homoglyphs and zero-width bypasses
        normalized = _normalize_text(message)
        if _INJECTION_RE.search(normalized):
            logger.warning("Prompt injection attempt detected (normalized)")
            return False, "Message contains disallowed content"

        return True, None


# ── Output Guardrails ─────────────────────────────────────────────────────────

_QUERY_PATTERNS = [
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\s+",
    r"\bMATCH\s*\(",
    r"\bMERGE\s*\(",
    r"\bRETURN\s+\w+",
    r"\bWHERE\s+\w+\.\w+\s*=",
]
_QUERY_RE = re.compile("|".join(_QUERY_PATTERNS), re.IGNORECASE)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

MAX_BLOCKS = 10


class OutputGuardrails:
    """Sanitizes agent responses before sending to client."""

    @staticmethod
    def sanitize(response: StructuredResponse) -> StructuredResponse:
        """Apply all output guardrails to a response."""
        # 1. Truncate to max blocks
        if len(response.blocks) > MAX_BLOCKS:
            response.blocks = response.blocks[:MAX_BLOCKS]
            logger.info("Truncated response from %d to %d blocks", len(response.blocks), MAX_BLOCKS)

        # 2. Sanitize text blocks
        sanitized_blocks: list[ContentBlock] = []
        for block in response.blocks:
            if block.type == "text":
                block.content.body = _strip_queries(block.content.body)
                block.content.body = _redact_pii(block.content.body)
            elif block.type == "list":
                for item in block.content.items:
                    item.text = _strip_queries(item.text)
            elif block.type == "code":
                pass  # Code blocks are intentionally raw — only shown in debug mode
            sanitized_blocks.append(block)

        response.blocks = sanitized_blocks

        # 3. Sanitize thinking steps
        response.thinking = [_strip_queries(t) for t in response.thinking]

        # 4. Sanitize follow-ups
        response.followUps = [_strip_queries(f) for f in response.followUps]

        # 5. Sanitize payload (strip queries and PII from string values)
        if response.payload is not None:
            response.payload = _sanitize_payload(response.payload)

        return response


_MAX_PAYLOAD_SIZE = 50_000  # 50KB limit on serialized payload
_MAX_PAYLOAD_DEPTH = 10  # Recursion depth limit to prevent stack overflow


def _sanitize_payload(payload: dict) -> dict:
    """Recursively sanitize string values in the payload dict.

    Strips SQL/Cypher queries and redacts PII from all string values.
    Enforces a 50KB size limit and 10-level recursion depth limit.
    """

    def _sanitize_value(v: object, depth: int = 0) -> object:
        if depth > _MAX_PAYLOAD_DEPTH:
            return "[nested content truncated]" if isinstance(v, (dict, list)) else v
        if isinstance(v, str):
            return _redact_pii(_strip_queries(v))
        if isinstance(v, dict):
            return {k: _sanitize_value(val, depth + 1) for k, val in v.items()}
        if isinstance(v, list):
            return [_sanitize_value(item, depth + 1) for item in v]
        return v

    # Sanitize first, then check size on the result
    sanitized = _sanitize_value(payload)

    try:
        serialized = json.dumps(sanitized, default=str)
        if len(serialized) > _MAX_PAYLOAD_SIZE:
            logger.warning("Payload too large (%d bytes), truncating", len(serialized))
            return {"error": "Payload exceeded size limit"}
    except (TypeError, ValueError):
        return {"error": "Payload serialization failed"}

    return sanitized  # type: ignore[return-value]


def _strip_queries(text: str) -> str:
    """Remove SQL/Cypher query fragments from text."""
    if _QUERY_RE.search(text):
        # Replace query-like content with a note
        lines = text.split("\n")
        clean_lines = [
            line for line in lines
            if not _QUERY_RE.search(line)
        ]
        return "\n".join(clean_lines).strip()
    return text


def _redact_pii(text: str) -> str:
    """Redact email addresses from text."""
    return _EMAIL_RE.sub("[email redacted]", text)
