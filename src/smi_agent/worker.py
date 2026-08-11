"""Temporal worker entry point for Smartinerary.

Each specialist agent (flight, hotel, restaurant, attraction, itinerary
generation) plus the core bookkeeping activities (persist_trip,
record_workflow_metric) is its own Temporal task queue — the microservice
boundary in this codebase. Set SMI_AGENT_QUEUE to pick which one this process
serves; the ItineraryWorkflow definition itself lives on its own queue,
separate from every activity, so scaling one agent never means scaling the
orchestrator or any other agent.

Start one worker per agent (see docker-compose.yml — each `worker-<agent>`
service sets SMI_AGENT_QUEUE to a different value)::

    SMI_AGENT_QUEUE=flight-search smi-agent-worker
    SMI_AGENT_QUEUE=hotel-search smi-agent-worker
    SMI_AGENT_QUEUE=orchestrator smi-agent-worker   # hosts ItineraryWorkflow, no activities

Leaving SMI_AGENT_QUEUE unset runs every agent plus the orchestrator in one
process — the old monolithic behaviour, useful for quick local dev against a
bare `temporal server start-dev` without docker-compose::

    PYTHONPATH=src python3 -m smi_agent.worker

Or via the installed script (after `pip install -e .`):

    smi-agent-worker

The relevant worker(s) must be running for workflows submitted via the
Temporal client or the API layer to actually execute.

Temporal server must be running locally::

    temporal server start-dev

This starts a local Temporal server on localhost:7233 with the Web UI at
localhost:8233.

Prometheus metrics for workflow/agent executions (this process, not the
FastAPI API) are served on SMI_WORKER_METRICS_PORT (default 9100):

    curl http://localhost:9100/metrics

This is a separate process from the conversation API, so it exposes its own
/metrics endpoint rather than sharing the API's — a Prometheus scrape config
targets both.
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow
from smi_agent.activities.task_queues import (
    ATTRACTION_QUEUE,
    CORE_SERVICES_QUEUE,
    FLIGHT_QUEUE,
    HOTEL_QUEUE,
    ITINERARY_GENERATION_QUEUE,
    RESTAURANT_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from smi_agent.activities.travel_activities import (
    attraction_search_activity,
    flight_search_activity,
    hotel_search_activity,
    itinerary_generation_activity,
    parse_trip_intent_activity,
    persist_trip_activity,
    record_ranking_feedback_activity,
    record_workflow_metric_activity,
    restaurant_search_activity,
)

logger = logging.getLogger(__name__)

# One entry per agent microservice. Keys double as SMI_AGENT_QUEUE values and
# as Temporal task queue names — itinerary_workflow.py routes each
# execute_activity() call to the matching queue from task_queues.py.
AGENT_QUEUES: dict[str, list] = {
    FLIGHT_QUEUE: [flight_search_activity],
    HOTEL_QUEUE: [hotel_search_activity],
    RESTAURANT_QUEUE: [restaurant_search_activity],
    ATTRACTION_QUEUE: [attraction_search_activity],
    ITINERARY_GENERATION_QUEUE: [itinerary_generation_activity, parse_trip_intent_activity],
    CORE_SERVICES_QUEUE: [persist_trip_activity, record_workflow_metric_activity, record_ranking_feedback_activity],
}

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
METRICS_PORT = int(os.environ.get("SMI_WORKER_METRICS_PORT", "9100"))


def _resolve_queue() -> tuple[str, list, list]:
    """Return (task_queue, workflows, activities) for SMI_AGENT_QUEUE.

    - "orchestrator": hosts ItineraryWorkflow only, on WORKFLOW_TASK_QUEUE.
    - one of AGENT_QUEUES: hosts that agent's activities only, on its own
      dedicated task queue.
    - unset: monolithic dev mode — workflow + every activity, one process,
      one queue (WORKFLOW_TASK_QUEUE) — matches the old worker.py behaviour.
    """
    agent = os.environ.get("SMI_AGENT_QUEUE")

    if agent is None:
        all_activities = [a for acts in AGENT_QUEUES.values() for a in acts]
        return WORKFLOW_TASK_QUEUE, [ItineraryWorkflow], all_activities

    if agent == "orchestrator":
        return WORKFLOW_TASK_QUEUE, [ItineraryWorkflow], []

    if agent not in AGENT_QUEUES:
        raise ValueError(
            f"Unknown SMI_AGENT_QUEUE={agent!r} — expected 'orchestrator' or one of "
            f"{sorted(AGENT_QUEUES)}"
        )

    return agent, [], AGENT_QUEUES[agent]


async def _run_worker() -> None:
    from prometheus_client import start_http_server

    start_http_server(METRICS_PORT)
    logger.info("Worker metrics server listening on :%d/metrics", METRICS_PORT)

    task_queue, workflows, activities = _resolve_queue()

    logger.info("Connecting to Temporal at %s", TEMPORAL_HOST)
    client = await Client.connect(TEMPORAL_HOST)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
    )

    logger.info(
        "Worker started — polling task queue '%s' (workflows=%s, activities=%s)",
        task_queue,
        [w.__name__ for w in workflows],
        [a.__name__ for a in activities],
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
