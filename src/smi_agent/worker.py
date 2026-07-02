"""Temporal worker entry point for Smartinerary.

Connects to the Temporal server, registers the ItineraryWorkflow and all four
activities, and starts polling the task queue.

Start the worker::

    # With environment variables loaded from .env:
    PYTHONPATH=src python3 -m smi_agent.worker

Or via the installed script (after `pip install -e .`):

    smi-agent-worker

The worker must be running for workflows submitted via the Temporal client
or the API layer to actually execute.

Temporal server must be running locally::

    temporal server start-dev

This starts a local Temporal server on localhost:7233 with the Web UI at
localhost:8233.
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from smi_agent.activities.itinerary_workflow import ItineraryWorkflow
from smi_agent.activities.travel_activities import (
    flight_search_activity,
    hotel_search_activity,
    itinerary_generation_activity,
    restaurant_search_activity,
)

logger = logging.getLogger(__name__)

TASK_QUEUE = "smartinerary"
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")


async def _run_worker() -> None:
    logger.info("Connecting to Temporal at %s", TEMPORAL_HOST)
    client = await Client.connect(TEMPORAL_HOST)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ItineraryWorkflow],
        activities=[
            flight_search_activity,
            hotel_search_activity,
            restaurant_search_activity,
            itinerary_generation_activity,
        ],
    )

    logger.info("Worker started — polling task queue '%s'", TASK_QUEUE)
    await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
