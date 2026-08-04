import type { ContentBlock } from "./types";

/**
 * Matches src/smi_agent/agents/supervisor_graph_adapter.py's
 * _serialize_for_persistence — supervisor-routed messages persist their real
 * blocks as JSON in the same `content` column every other agent uses for
 * plain prose, tagged with `kind` so the two are never confused.
 */
const STRUCTURED_CONTENT_KIND = "smi_structured_message_v1";

interface StructuredContentPayload {
  kind: typeof STRUCTURED_CONTENT_KIND;
  blocks: ContentBlock[];
}

/**
 * Parses a message's `content` as structured blocks, if it is one. Accepts
 * `unknown` (not just string) because the API layer's SafePostgresExecutor
 * auto-parses any TEXT column value that looks like JSON — see
 * postgres_client/safe_executor.py's _deserialize_jsonb, which is
 * column-agnostic, not scoped to real jsonb columns — so by the time this
 * reaches the browser, `content` may already be a plain object instead of
 * the JSON string ConversationMessageItem's type declares.
 *
 * Returns null for ordinary plain-text messages (the common case, from every
 * non-supervisor agent) — callers should render `content` as plain text then.
 */
export function parseStructuredContent(content: unknown): ContentBlock[] | null {
  let payload: unknown = content;

  if (typeof content === "string") {
    if (!content.startsWith("{")) return null; // cheap reject before JSON.parse
    try {
      payload = JSON.parse(content);
    } catch {
      return null; // Not JSON — an ordinary plain-text message.
    }
  }

  if (payload && typeof payload === "object") {
    const candidate = payload as Partial<StructuredContentPayload>;
    if (candidate.kind === STRUCTURED_CONTENT_KIND && Array.isArray(candidate.blocks)) {
      return candidate.blocks;
    }
  }
  return null;
}

/** Encodes blocks the same way, for locally-built optimistic messages
 * (useChat's "done" handler) so live and reloaded messages render identically.
 */
export function encodeStructuredContent(blocks: ContentBlock[]): string {
  return JSON.stringify({ kind: STRUCTURED_CONTENT_KIND, blocks });
}
