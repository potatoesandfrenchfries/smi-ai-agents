"""Task queue names for the per-agent Temporal worker split.

Single source of truth shared by ``smi_agent.worker`` (which activities a
given worker process registers) and ``smi_agent.activities.itinerary_workflow``
(which queue each ``execute_activity`` call is routed to) — keeping them in
one module avoids the two ever drifting out of sync, and avoids a circular
import between worker.py and itinerary_workflow.py (worker.py already imports
ItineraryWorkflow from the latter).
"""

from __future__ import annotations

# Workflow's own task queue — what clients (gateway, scripts/*.py) pass to
# execute_workflow(). Unaffected by activity-level queue routing below.
WORKFLOW_TASK_QUEUE = "smartinerary"

# One task queue per agent microservice.
FLIGHT_QUEUE = "flight-search"
HOTEL_QUEUE = "hotel-search"
RESTAURANT_QUEUE = "restaurant-search"
ATTRACTION_QUEUE = "attraction-search"
# Hosts the full LangGraph pipeline (parse_intent, merge_results,
# policy_check, budget_agent, compile_itinerary, reflect_itinerary) — the
# intent-classification and orchestration logic in the active pipeline.
ITINERARY_GENERATION_QUEUE = "itinerary-generation"
# Bookkeeping, not an agent — grouped onto one queue rather than split
# further since neither does meaningful independent work worth scaling.
CORE_SERVICES_QUEUE = "core-services"
