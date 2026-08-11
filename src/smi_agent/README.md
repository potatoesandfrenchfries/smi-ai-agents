# smi_agent

Python service for Smartinerary. One codebase, two entry points — see
[pyproject.toml](../../pyproject.toml) `[project.scripts]`:

| Entry point | Process | Runs |
| --- | --- | --- |
| `smi-conversation-worker` | `api` | FastAPI app (`api/app.py`) — chat endpoints, conversation CRUD, the supervisor/specialist agent orchestrator |
| `smi-agent-worker` | `worker` | Temporal worker (`worker.py`) — the LangGraph itinerary planning workflow and its activities |

## Two orchestration paths — don't conflate them

- **`agents/` + `conversation/`** (FastAPI, live over HTTP): `SupervisorAgent`
  (`agents/supervisor.py`) classifies intent and delegates to specialist
  agents (`agents/runtime.py`, `agents/registry.py`) via plain Python control
  flow — no LangGraph. The `/chat` and `/api/v1/conversations/*/chat`
  endpoints instead run a small LangGraph graph
  (`conversation/graph.py`: `context_inject → compact/generate`) for
  page-context-aware chat. Routing decisions log to `logs/planner_trace.log`
  (`observability/logging.py::get_planner_trace_logger`).
- **`graph/itinerary_graph.py`** (Temporal only): the real `StateGraph`
  6-stage itinerary planning workflow (parse_intent → specialist search →
  merge_results → policy_check → compile_itinerary → reflect_itinerary →
  await_confirmation). It's invoked exclusively from
  `activities/travel_activities.py` inside `ItineraryWorkflow`
  (`activities/itinerary_workflow.py`) — reachable via the Temporal client
  (gateway, `scripts/`), never directly over HTTP.

## Module map

| Module | Responsibility |
| --- | --- |
| `api/` | FastAPI app, request/response models, conversation + investigation runners |
| `agents/` | Supervisor + specialist agent orchestration (non-LangGraph chat path) |
| `graph/` | LangGraph `StateGraph` itinerary planning workflow + state schema |
| `conversation/` | LangGraph chat graph, session store, SSE streaming, tool registry |
| `activities/` | Temporal activities and workflow definitions |
| `providers/` | Flight/hotel/restaurant/attraction search provider interfaces + implementations |
| `domain/` | Domain registry (pluggable entity resolver, template provider — currently `examples/travel`) |
| `examples/travel/` | The travel domain's concrete tools, entity resolver, Cypher query bindings |
| `llm/` | LLM router (provider/model selection) and prompt template loading |
| `neo4j_client/` | Neo4j driver + Cypher template loader |
| `postgres_client/` | Postgres client and `SafePostgresExecutor` (allowlisted parameterized queries only) |
| `cache/` | Redis-backed caching |
| `checkpoint/` | LangGraph checkpointer |
| `trip_store/` | Confirmed-trip persistence (file-backed) |
| `streaming/` | SSE step emitters for progress events |
| `observability/` | Structured logging, OTel tracing, Langfuse — see note below |
| `config/` | Agent definition loader (`agent_definitions/*.yaml`), Redis key helpers, defaults |
| `schemas/` | Shared pydantic schemas |
| `utils/` | Misc helpers |
| `worker.py` | Temporal worker entry point — see per-agent task queue split below |

## Temporal worker task queues (`worker.py`)

Each specialist agent's activities run on their own task queue, set via
`SMI_AGENT_QUEUE`, so any one agent can be scaled or deployed independently
of the others and of the workflow orchestrator:

```bash
SMI_AGENT_QUEUE=flight-search      smi-agent-worker
SMI_AGENT_QUEUE=hotel-search       smi-agent-worker
SMI_AGENT_QUEUE=restaurant-search  smi-agent-worker
SMI_AGENT_QUEUE=attraction-search  smi-agent-worker
SMI_AGENT_QUEUE=itinerary-generation smi-agent-worker
SMI_AGENT_QUEUE=core-services       smi-agent-worker
SMI_AGENT_QUEUE=orchestrator        smi-agent-worker   # hosts ItineraryWorkflow only
```

Unset `SMI_AGENT_QUEUE` runs everything in one process (monolithic dev mode).

## Observability

- `observability/logging.py` — structlog JSON logging to `logs/smi-agent.log`,
  plus a dedicated plain-text `logs/planner_trace.log` for the supervisor/
  planner routing path.
- `observability/tracing.py` (OTel) and `observability/langfuse.py` — fully
  implemented but **not currently wired up**: `configure_tracing()` and
  `configure_langfuse()` are never called at process startup, so no spans are
  emitted today.
- `observability/metrics.py` — Prometheus metrics; `api` exposes `/metrics`,
  the worker exposes its own on `SMI_WORKER_METRICS_PORT` (default 9100).

## Ranking feedback loop

- `providers/ranking/` — accept/reject, ratings, and candidate-swap feedback
  (captured via signals on `activities/itinerary_workflow.py`) flow through
  `record_ranking_feedback_activity` (`activities/travel_activities.py`) into
  `bandit.py`'s weight updates. **Budget-change feedback is not wired**: the
  workflow signal handler has a `section == "budget"` branch ready for it,
  but the gateway's `changesSchema`/`ItineraryEditRequest`
  (`services/gateway/src/routes/trips.ts`, `temporal/client.ts`) only accept
  a `candidateId` field, so no live caller can send a budget edit yet.

## Local dev

```bash
PYTHONPATH=src python3 -m smi_agent.worker      # or: smi-agent-worker
PYTHONPATH=src uvicorn smi_agent.api.app:app --reload
```

Requires Postgres, Redis, Neo4j, and (for the worker) a local Temporal server
(`temporal server start-dev`) — or run the full stack via `docker compose up`
from the repo root.
