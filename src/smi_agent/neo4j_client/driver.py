"""Neo4j async driver wrapper.

Provides a thin ``Neo4jDriver`` class around the official ``neo4j`` async driver.
The wrapper is dependency-injectable: tests pass a mock that implements the
same interface without a live Neo4j instance.

Environment variables (all optional; can also be passed as constructor args):
    NEO4J_URI           bolt://localhost:7687
    NEO4J_USER          neo4j
    NEO4J_PASSWORD      changeme
    NEO4J_DATABASE      neo4j
    NEO4J_POOL_SIZE     10

Usage::

    async with Neo4jDriver.from_env() as driver:
        records = await driver.run_query(
            "MATCH (e:Exposure {id: $id}) RETURN e",
            parameters={"id": "EXP-001"},
            timeout=5.0,
        )
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class Neo4jDriverError(RuntimeError):
    """Wraps Neo4j driver errors with context."""


class Neo4jDriver:
    """Thin async wrapper around ``neo4j.AsyncGraphDatabase``.

    All queries run in a fresh session (no transaction pinning).  The
    underlying connection pool is managed by the driver.

    This class is intentionally not a Pydantic model — it holds an open
    connection pool which is not serializable.
    """

    def __init__(
        self,
        uri: str,
        auth: tuple[str, str],
        database: str = "neo4j",
        max_connection_pool_size: int = 10,
    ) -> None:
        try:
            import neo4j  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError("neo4j package is required. Install it: pip install neo4j") from exc

        self._database = database
        self._driver = neo4j.AsyncGraphDatabase.driver(
            uri,
            auth=auth,
            max_connection_pool_size=max_connection_pool_size,
        )
        logger.debug("Neo4jDriver initialised: uri=%s database=%s", uri, database)

    @classmethod
    def from_env(cls) -> Neo4jDriver:
        """Construct from environment variables."""
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD")
        if not password:
            raise ValueError("NEO4J_PASSWORD environment variable is required but not set")
        database = os.environ.get("NEO4J_DATABASE", "neo4j")
        pool_size = int(os.environ.get("NEO4J_POOL_SIZE", "10"))
        return cls(
            uri=uri,
            auth=(user, password),
            database=database,
            max_connection_pool_size=pool_size,
        )

    async def run_query(
        self,
        cypher: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> list[dict[str, Any]]:
        """Run a read query and return records as plain dicts.

        Args:
            cypher: The Cypher query string (no raw-Cypher from LLM — only
                pre-validated template text reaches this method).
            parameters: Cypher parameter dict (interpolated params already
                substituted by the template renderer).
            timeout: Wall-clock timeout in seconds.  The query is cancelled
                via ``asyncio.wait_for`` if it exceeds this.

        Returns:
            List of record dicts.  Each dict key is the RETURN alias.

        Raises:
            Neo4jDriverError: On query failure or timeout.
        """
        try:
            return await asyncio.wait_for(
                self._run_query_inner(cypher, parameters),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise Neo4jDriverError(f"Query timed out after {timeout}s") from exc
        except Exception as exc:
            # Log full details internally; surface only type to callers
            # to avoid leaking query text or Neo4j server internals.
            logger.debug("Neo4j query error detail: %s", exc)
            raise Neo4jDriverError(
                f"Query failed: {type(exc).__name__} (see debug logs for detail)"
            ) from exc

    async def _run_query_inner(
        self,
        cypher: str,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, parameters=parameters)
            return await result.data()

    async def verify_connectivity(self) -> None:
        """Raise ``Neo4jDriverError`` if the server is unreachable."""
        try:
            await self._driver.verify_connectivity()
        except Exception as exc:
            raise Neo4jDriverError(f"Neo4j connectivity check failed: {exc}") from exc

    async def close(self) -> None:
        await self._driver.close()
        logger.debug("Neo4jDriver closed.")

    async def __aenter__(self) -> Neo4jDriver:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
