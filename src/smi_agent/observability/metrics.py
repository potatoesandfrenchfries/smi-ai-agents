"""Central Prometheus metric definitions.

Every counter/histogram used anywhere in the app is defined once here so
names stay stable across dashboards/alerts. Two separate OS processes emit
these: the FastAPI conversation API (api_requests/http latency) and the
Temporal worker (workflow/agent executions, their latency and errors) —
each process exposes its own /metrics endpoint (see api/app.py and
worker.py) since they don't share memory.

track_agent_execution() is safe to use inside a Temporal *activity* (each
execution is a real, at-least-once side effect) but must never be called
directly inside workflow.run() — Temporal replays workflow code from
history, which would double-count. Workflow-level counts are instead
recorded via record_workflow_metric_activity (activities/travel_activities.py),
called explicitly by the workflow like any other activity.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

API_REQUESTS_TOTAL = Counter(
    "smi_api_requests_total",
    "Total HTTP requests handled by the conversation API",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "smi_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)

WORKFLOW_EXECUTIONS_TOTAL = Counter(
    "smi_workflow_executions_total",
    "Total Temporal workflow executions by terminal status",
    ["workflow", "status"],
)

AGENT_EXECUTIONS_TOTAL = Counter(
    "smi_agent_executions_total",
    "Total agent/activity executions by outcome",
    ["agent", "status"],
)

AGENT_EXECUTION_DURATION_SECONDS = Histogram(
    "smi_agent_execution_duration_seconds",
    "Agent/activity execution latency in seconds",
    ["agent"],
)

ERRORS_TOTAL = Counter(
    "smi_errors_total",
    "Total errors by originating component",
    ["component"],
)


@contextmanager
def track_agent_execution(agent_name: str) -> Iterator[None]:
    """Records execution count, latency, and error count for one agent/activity call."""
    start = time.perf_counter()
    try:
        yield
    except Exception:
        AGENT_EXECUTIONS_TOTAL.labels(agent=agent_name, status="error").inc()
        ERRORS_TOTAL.labels(component=agent_name).inc()
        raise
    else:
        AGENT_EXECUTIONS_TOTAL.labels(agent=agent_name, status="success").inc()
    finally:
        AGENT_EXECUTION_DURATION_SECONDS.labels(agent=agent_name).observe(time.perf_counter() - start)
