"""SafePostgresExecutor — canned-query-only Postgres access.

Mirrors ``SafeCypherExecutor``: double-gate allowlist (agent config + query
level), tracks query count, refuses anything not in the registry.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from smi_agent.postgres_client.client import PostgresClient
from smi_agent.postgres_client.queries import SqlQuery, get_domain_queries

logger = logging.getLogger(__name__)


class QueryNotAllowedError(Exception):
    """Raised when a query is not permitted for this agent."""


class SafePostgresExecutor:
    """Execute canned Postgres queries with allowlist enforcement."""

    def __init__(
        self,
        client: PostgresClient,
        allowed_queries: list[str],
        agent_name: str,
    ) -> None:
        self._client = client
        self._allowed_queries = frozenset(allowed_queries)
        self._agent_name = agent_name
        self.queries_executed: int = 0

    def _resolve(self, query_name: str) -> SqlQuery:
        """Resolve and authorize a query by name.

        Checks both the generic registry and domain-specific queries.
        """
        full_registry = get_domain_queries()
        query = full_registry.get(query_name)
        if query is None:
            raise QueryNotAllowedError(
                f"Query {query_name!r} is not registered in the query registry"
            )
        if query_name not in self._allowed_queries:
            raise QueryNotAllowedError(
                f"Query {query_name!r} is not in the allowed list for agent {self._agent_name!r}"
            )
        if "*" not in query.allowed_agents and self._agent_name not in query.allowed_agents:
            raise QueryNotAllowedError(
                f"Agent {self._agent_name!r} is not permitted to run query {query_name!r}"
            )
        return query

    async def run(self, query_name: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a read query and return rows as dicts.

        JSONB columns are automatically parsed from strings to dicts/lists.
        """
        query = self._resolve(query_name)
        self.queries_executed += 1
        rows = await self._client.run_query(query.sql, *args)
        return [_deserialize_jsonb(row) for row in rows]

    async def execute(self, query_name: str, *args: Any) -> str | None:
        """Execute a write query and return the first column of the first row."""
        query = self._resolve(query_name)
        self.queries_executed += 1
        return await self._client.run_execute(query.sql, *args)

    async def run_transaction(
        self,
        steps: list[tuple[str, tuple[Any, ...]]],
    ) -> None:
        """Execute multiple named queries in a single transaction.

        Args:
            steps: List of (query_name, args) tuples.

        All queries are resolved and authorized before any are executed.
        """
        resolved: list[tuple[str, tuple[Any, ...]]] = []
        for query_name, args in steps:
            query = self._resolve(query_name)
            resolved.append((query.sql, args))
            self.queries_executed += 1

        await self._client.run_transaction(resolved)

    async def run_transaction_returning(
        self,
        steps: list[tuple[str, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Execute named queries in a transaction; return rows from the last statement.

        All queries are resolved and authorized before any are executed.
        The last statement should have a RETURNING clause.
        """
        resolved: list[tuple[str, tuple[Any, ...]]] = []
        for query_name, args in steps:
            query = self._resolve(query_name)
            resolved.append((query.sql, args))
            self.queries_executed += 1

        rows = await self._client.run_transaction_returning(resolved)
        return [_deserialize_jsonb(row) for row in rows]


def _deserialize_jsonb(row: dict[str, Any]) -> dict[str, Any]:
    """Parse string-encoded JSONB columns into Python objects."""
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, str) and value.startswith(("{", "[")):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                result[key] = json.loads(value)
    return result
