# Smartinerary (smi-ai-agents)

AI travel-planning agent: a conversational interface plus a multi-agent
itinerary planner (flights, hotels, restaurants, attractions) with a
human-in-the-loop confirmation gate before anything is booked.

## Architecture

```text
web (React)  →  gateway (Node/Express)  →  api (FastAPI)        chat, conversation CRUD
                        │                        │
                        └──────────────► temporal ◄──────────────┐
                                            │                     │
                                   worker-orchestrator     worker-{flight,hotel,
                                   (ItineraryWorkflow)      restaurant,attraction,
                                            │               itinerary,core}
                                            ▼
                              Postgres · Neo4j · Redis
```

- **web/** — React/TypeScript console (chat UI, itinerary review).
- **services/gateway/** — Node/TypeScript BFF. Proxies conversation traffic to
  `api` and submits/queries itinerary workflows on Temporal for the HITL
  review flow.
- **src/smi_agent/** — the Python service. Ships as two entry points from one
  codebase: `smi-conversation-worker` (FastAPI `api` process — chat,
  conversation persistence, the supervisor/specialist agent orchestration)
  and `smi-agent-worker` (Temporal worker — runs the LangGraph itinerary
  planning workflow and its activities). See
  [src/smi_agent/README.md](src/smi_agent/README.md).
- **Temporal** — coordinates the itinerary planning workflow. Each specialist
  agent (flight/hotel/restaurant/attraction/itinerary-generation/core) runs
  on its own task queue, so it can be scaled and deployed independently of
  the others and of the workflow orchestrator — see `worker.py`'s
  `SMI_AGENT_QUEUE`.
- **Postgres** — conversations, trips, tenancy/config (`db/README.md`).
- **Neo4j** — domain graph queries (locations, provider search) via
  parameterized Cypher templates (`cypher/`).
- **Redis** — session store, SSE pub/sub, search-result cache.

Two independent chat/planning paths exist today and should not be confused:
the FastAPI `/api/v1/chat` endpoint runs a hand-rolled supervisor/specialist
orchestrator (`src/smi_agent/agents/`), while the actual LangGraph
`StateGraph` itinerary workflow (`src/smi_agent/graph/itinerary_graph.py`)
only runs inside the Temporal worker, driven by `ItineraryWorkflow`.

## Repo layout

| Path | Purpose |
| --- | --- |
| `src/smi_agent/` | Python service (API + Temporal worker) |
| `services/gateway/` | Node/TypeScript REST gateway |
| `web/` | React frontend |
| `db/migrations/` | Postgres schema, applied in order on first init |
| `scripts/` | Local CLIs for exercising the planner without the web UI |
| `agent_definitions/` | YAML config per agent (prompts dir, tool allowlist, ceilings) |
| `prompts/` | LLM prompt templates, one subdir per agent |
| `cypher/` | Parameterized Neo4j query templates |
| `data/` | Local file-backed stores (trip persistence, in-progress plans) |

## Running locally

```bash
docker compose up --build
```

Brings up Postgres, Redis, Neo4j, Temporal (+ Web UI on :8233), the `api`,
`worker` (or per-agent `worker-*` services), `gateway`, and `web`. Open
http://localhost:5173. Requires an LLM provider key in `.env` (see
`.env.example`).

For a terminal-only planner loop without the web stack, see
[scripts/README.md](scripts/README.md).
