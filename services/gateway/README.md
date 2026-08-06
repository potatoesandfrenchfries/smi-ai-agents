# gateway

Node/TypeScript REST gateway — the single HTTP entry point the `web`
frontend talks to. Two responsibilities:

1. **Conversation proxy** — forwards chat/conversation requests to the
   Python `api` service (`CONVERSATION_API_URL`).
2. **Itinerary/HITL surface over Temporal** — submits `ItineraryWorkflow`
   executions and relays human-in-the-loop review/edit/confirm signals
   directly against the Temporal server (`TEMPORAL_ADDRESS`), rather than
   through the Python API.

## Layout

| Path | Purpose |
| --- | --- |
| `src/index.ts` | Express app setup, middleware, route mounting |
| `src/routes/conversations.ts` | Proxies to the FastAPI conversation API |
| `src/routes/trips.ts` | Submits/queries `ItineraryWorkflow` on Temporal |
| `src/temporal/client.ts` | Temporal client construction |
| `src/middleware/auth.ts` | Auth header extraction/validation |
| `src/config/env.ts` | Environment variable loading/validation |

## Configuration

| Env var | Purpose |
| --- | --- |
| `GATEWAY_PORT` | Port to listen on (default 4000) |
| `CONVERSATION_API_URL` | Upstream FastAPI `api` service URL |
| `TEMPORAL_ADDRESS` | Temporal frontend address |
| `TEMPORAL_NAMESPACE` | Temporal namespace (default `default`) |
| `SMI_TASK_QUEUE` | Task queue `ItineraryWorkflow` is submitted on |
| `GATEWAY_CORS_ORIGIN` | Allowed CORS origin for the `web` frontend |

## Local dev

```bash
npm install
npm run dev        # tsx watch, no build step
npm run typecheck
npm run build && npm start
```

Requires the `api` service and a Temporal server reachable at
`TEMPORAL_ADDRESS`. See the repo root `docker-compose.yml` to run the full
stack instead.
