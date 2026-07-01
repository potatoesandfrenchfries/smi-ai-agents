"""Audit trail logging for the multi-agent chat system.

Every chat interaction is logged with: who asked, which agent handled,
which tools were called, what data was accessed, and the response shape.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from smi_agent.agents.response import StructuredResponse

logger = logging.getLogger(__name__)


class AuditRecord(BaseModel):
    """Audit trail record for a single chat interaction."""

    request_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    user_id: str | None = None
    tenant_id: str | None = None
    user_message: str = ""
    agent: str = ""
    tools_invoked: list[str] = Field(default_factory=list)
    response_block_types: list[str] = Field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = "success"


class AuditLogger:
    """Logs audit records to Postgres with retry and task tracking.

    Tasks are tracked in ``_pending_tasks`` so they survive until completion
    and are awaited during graceful shutdown.
    """

    _pending_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def from_response(
        response: StructuredResponse,
        user_message: str,
    ) -> AuditRecord:
        """Build an audit record from a structured response."""
        return AuditRecord(
            request_id=response.meta.requestId,
            user_id=response.meta.userId,
            tenant_id=response.meta.tenantId,
            user_message=user_message[:500],
            agent=response.agent,
            tools_invoked=response.meta.toolsUsed,
            response_block_types=[b.type for b in response.blocks],
            tokens_used=response.meta.tokensUsed,
            cost_usd=response.meta.costUsd,
            duration_ms=response.meta.durationMs,
            status=response.status,
        )

    @staticmethod
    async def log_async(
        pg_executor: Any | None,
        record: AuditRecord,
        max_retries: int = 2,
    ) -> None:
        """Log audit record to Postgres with retry. Best-effort, never raises."""
        if pg_executor is None:
            logger.debug("Audit: no pg_executor, logging to file only")
            logger.info(
                "AUDIT req=%s user=%s tenant=%s agent=%s status=%s duration=%dms",
                record.request_id, record.user_id, record.tenant_id,
                record.agent, record.status, record.duration_ms,
            )
            return

        for attempt in range(max_retries + 1):
            try:
                await pg_executor.execute(
                    "insert_audit_log",
                    record.request_id,
                    record.user_id,
                    record.tenant_id,
                    record.agent,
                    record.user_message,
                    record.status,
                    record.duration_ms,
                    record.tokens_used,
                    record.cost_usd,
                )
                return  # Success
            except Exception:
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))  # Backoff
                else:
                    logger.warning("Audit log write failed after %d attempts", max_retries + 1, exc_info=True)
                    # Fallback: log to file so data is not lost
                    logger.info(
                        "AUDIT_FALLBACK req=%s user=%s tenant=%s agent=%s status=%s",
                        record.request_id, record.user_id, record.tenant_id,
                        record.agent, record.status,
                    )

    @staticmethod
    def log_fire_and_forget(
        pg_executor: Any | None,
        record: AuditRecord,
    ) -> None:
        """Fire-and-forget audit log — tracks task to prevent GC/loss."""
        task = asyncio.create_task(AuditLogger.log_async(pg_executor, record))
        AuditLogger._pending_tasks.add(task)
        task.add_done_callback(AuditLogger._pending_tasks.discard)

    @staticmethod
    async def flush_pending() -> None:
        """Await all pending audit tasks. Call during graceful shutdown."""
        if AuditLogger._pending_tasks:
            await asyncio.gather(*AuditLogger._pending_tasks, return_exceptions=True)
            AuditLogger._pending_tasks.clear()
