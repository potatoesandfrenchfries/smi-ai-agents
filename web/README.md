# web

React/TypeScript console — the Smartinerary UI. Chat interface plus
itinerary review (human-in-the-loop confirm/edit before booking).

Talks only to the `gateway` service (`VITE_GATEWAY_URL`); never calls the
Python `api` or Temporal directly.

## Layout

| Path | Purpose |
| --- | --- |
| `src/api/client.ts` | HTTP client for the gateway REST API |
| `src/api/sse.ts` | Server-sent events client (chat streaming, reasoning steps) |
| `src/api/structuredContent.ts` | Parses structured response blocks from the supervisor/specialist agents |
| `src/api/mock.ts` | Mock data for local UI development without a backend |
| `src/hooks/useChat.ts` | Chat state + SSE streaming |
| `src/hooks/useConversations.ts` | Conversation list/CRUD |
| `src/hooks/useItinerary.ts` | Itinerary polling + HITL review actions |
| `src/components/conversation/` | Chat UI |
| `src/components/itinerary/` | Itinerary review/confirm UI |
| `src/components/layout/`, `common/` | Shared layout and UI primitives |

## Local dev

```bash
npm install
npm run dev         # http://localhost:5173
npm run lint
npm run build
```

Requires `VITE_GATEWAY_URL` pointing at a running `gateway` (default
`http://localhost:4000`). See the repo root `docker-compose.yml` to run the
full stack instead.
