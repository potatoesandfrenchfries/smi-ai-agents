"""LangGraph AsyncPostgresSaver — checkpoint setup and teardown.

Provides ``get_checkpointer()`` which returns a configured
``AsyncPostgresSaver``.  The checkpointer's ``setup()`` method creates the
required tables (idempotent — safe to call on every worker startup).

Connection string is read from the ``LANGGRAPH_CHECKPOINT_DB_URL`` environment
variable.  If the variable is not set a ``ConfigurationError`` is raised
immediately rather than deferring the failure to the first checkpoint write.

Usage (in worker bootstrap)::

    async with get_checkpointer() as checkpointer:
        await checkpointer.setup()
        graph = build_agent_graph(defn, driver, loader, checkpointer=checkpointer)
        result = await graph.ainvoke(state)
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

_DB_URL_ENV = "LANGGRAPH_CHECKPOINT_DB_URL"


class CheckpointerConfigError(RuntimeError):
    """Raised when the checkpointer cannot be configured."""


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator:
    """Async context manager that yields a ready-to-use ``AsyncPostgresSaver``.

    Reads ``LANGGRAPH_CHECKPOINT_DB_URL`` from the environment.
    Calls ``checkpointer.setup()`` inside the context so tables are created
    before yielding control to the caller.

    Example::

        async with get_checkpointer() as cp:
            await cp.setup()
            graph = build_agent_graph(defn, driver, loader, checkpointer=cp)

    Raises:
        CheckpointerConfigError: ``LANGGRAPH_CHECKPOINT_DB_URL`` not set or
            ``langgraph-checkpoint-postgres`` package not available.
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise CheckpointerConfigError(
            "langgraph-checkpoint-postgres is not installed. "
            "Run: pip install langgraph-checkpoint-postgres"
        ) from exc

    db_url = os.environ.get(_DB_URL_ENV)
    if not db_url:
        raise CheckpointerConfigError(
            f"Environment variable '{_DB_URL_ENV}' is not set. "
            "Set it to a PostgreSQL connection string before starting the worker."
        )

    # AsyncPostgresSaver uses psycopg directly — strip SQLAlchemy driver prefix if present
    psycopg_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    logger.info("Initialising Postgres checkpointer from %s", _DB_URL_ENV)

    async with AsyncPostgresSaver.from_conn_string(psycopg_url) as checkpointer:
        await checkpointer.setup()
        logger.info("Postgres checkpointer ready")
        yield checkpointer
