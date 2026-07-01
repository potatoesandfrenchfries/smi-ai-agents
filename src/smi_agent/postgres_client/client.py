"""Async Postgres client — thin wrapper around asyncpg with connection pooling.

Mirrors the ``Neo4jDriver`` pattern: created once at worker/API startup, shared
across all activity invocations.

Environment variable:
    SMI_POSTGRES_URL   PostgreSQL DSN  (e.g. postgresql://smi:smi@localhost:5432/smi_agent)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class PostgresClient:
    """Async Postgres client backed by an ``asyncpg`` connection pool.

    Pool configuration via environment variables:
        PG_POOL_MIN_SIZE    Minimum connections kept open (default: 3)
        PG_POOL_MAX_SIZE    Maximum connections allowed (default: 15)

    Call ``await client.warmup()`` at startup to pre-create connections
    so the first query is fast.
    """

    def __init__(self, dsn: str, *, min_size: int | None = None, max_size: int | None = None) -> None:
        self._dsn = dsn
        self._min_size = min_size or int(os.environ.get("PG_POOL_MIN_SIZE", "3"))
        self._max_size = max_size or int(os.environ.get("PG_POOL_MAX_SIZE", "15"))
        self._pool: asyncpg.Pool | None = None

    @classmethod
    def from_env(cls) -> PostgresClient:
        """Create a client from the ``SMI_POSTGRES_URL`` environment variable."""
        dsn = os.environ.get("SMI_POSTGRES_URL", "")
        if not dsn:
            raise ValueError("SMI_POSTGRES_URL environment variable is not set")
        return cls(dsn)

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=30,
            )
            logger.info(
                "Postgres connection pool created (min=%d, max=%d)", self._min_size, self._max_size
            )
        return self._pool

    async def warmup(self) -> None:
        """Pre-create the connection pool at startup so first query is fast."""
        pool = await self._ensure_pool()
        logger.info("Postgres pool warmed up (%d/%d connections)", pool.get_size(), self._max_size)

    async def run_query(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as list of dicts."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]

    async def run_execute(self, sql: str, *args: Any) -> str | None:
        """Execute an INSERT/UPDATE and return the first column of the first row (if any)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
            if row is not None and len(row) > 0:
                return row[0]
            return None

    async def run_transaction(
        self,
        statements: list[tuple[str, tuple[Any, ...]]],
    ) -> None:
        """Execute multiple statements in a single transaction.

        Args:
            statements: List of (sql, args) tuples to execute sequentially.

        Raises:
            Exception: If any statement fails, the entire transaction is rolled back.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            for sql, args in statements:
                await conn.execute(sql, *args)

    async def run_transaction_returning(
        self,
        statements: list[tuple[str, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Execute statements in a transaction; return rows from the LAST statement.

        Use when the final statement has a RETURNING clause whose results you need.
        All prior statements are executed via ``execute()``, the last via ``fetch()``.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            for sql, args in statements[:-1]:
                await conn.execute(sql, *args)
            last_sql, last_args = statements[-1]
            rows = await conn.fetch(last_sql, *last_args)
            return [dict(r) for r in rows]

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Postgres connection pool closed")

    async def __aenter__(self) -> PostgresClient:
        await self._ensure_pool()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
