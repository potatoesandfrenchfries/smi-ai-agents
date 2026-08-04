/**
 * Environment configuration. Every value has a dev-friendly default so
 * `npm run dev` works against a local docker-compose stack with no .env file,
 * mirroring the Python side's convention (see Makefile's `.env` include).
 */

function optional(name: string, fallback: string): string {
  return process.env[name] ?? fallback;
}

export const env = {
  port: Number(optional("GATEWAY_PORT", "4000")),

  // The existing FastAPI conversation service (src/smi_agent/api/app.py).
  // The gateway proxies conversation CRUD + chat streaming to it rather than
  // reimplementing conversation persistence, LangGraph orchestration, or
  // guardrails in a second language.
  conversationApiUrl: optional("CONVERSATION_API_URL", "http://localhost:8080"),

  // Temporal server the itinerary workflow (src/smi_agent/activities/itinerary_workflow.py)
  // runs against. @temporalio/client talks to the same server as the Python
  // worker — Temporal's client protocol is language-agnostic, so signalling
  // and querying a Python-authored workflow from a Node client is a supported
  // cross-language pattern, not a workaround.
  temporalAddress: optional("TEMPORAL_ADDRESS", "localhost:7233"),
  temporalNamespace: optional("TEMPORAL_NAMESPACE", "default"),

  // Matches the Temporal task queue name the Python worker listens on
  // (Makefile: "Starting Temporal worker (task queue: smartinerary)").
  taskQueue: optional("SMI_TASK_QUEUE", "smartinerary"),

  corsOrigin: optional("GATEWAY_CORS_ORIGIN", "http://localhost:5173"),

  logLevel: optional("LOG_LEVEL", "info"),
};
