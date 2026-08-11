/**
 * TypeScript mirrors of the Pydantic contracts served by the Python
 * conversation API (src/smi_agent/api/models.py, agents/response.py) and the
 * gateway's own itinerary surface (services/gateway/src/routes/trips.ts).
 * Field names are kept exactly as the wire format sends them (camelCase, to
 * match the Pydantic models' aliasing) rather than reformatted to a
 * TS-idiomatic shape, so a payload can be typed without a translation layer.
 */

// ── Conversations ──────────────────────────────────────────────────────────

export interface ConversationContext {
  type: string;
  entityId?: string | null;
  label?: string | null;
}

export interface ConversationCeiling {
  maxMessages: number;
  maxTokens: number;
  messagesUsed: number;
  tokensUsed: number;
  messagesRemaining: number;
  tokensRemaining: number;
  percentUsed: number;
  status: "ok" | "warning" | "critical" | "exceeded";
  statusMessage?: string | null;
  hitAt?: string | null;
  hitReason?: string | null;
}

export interface ConversationLastMessage {
  role: string;
  preview: string;
  at: string;
}

export interface ConversationListItem {
  id: string;
  displayId: string;
  title: string | null;
  status: "ACTIVE" | "CEILING_HIT" | "CLOSED" | "ARCHIVED";
  agentName: string;
  context: ConversationContext | null;
  ceiling: ConversationCeiling;
  lastMessage: ConversationLastMessage | null;
  messageCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface ConversationMessageItem {
  id: string;
  seq: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  tokens: number | null;
  createdAt: string;
}

export interface Paginated<T> {
  data: T[];
  pagination: { total: number; offset: number; limit: number };
}

// ── Structured response envelope (agents/response.py) ─────────────────────

export type BlockType =
  | "text"
  | "table"
  | "entity_card"
  | "graph"
  | "metric_row"
  | "list"
  | "action_buttons"
  | "code"
  | "error"
  | "separator";

export interface Highlight {
  term: string;
  entityType: string;
  entityId: string;
}

export interface TextContent {
  body: string;
  highlights?: Highlight[];
}

export interface TableColumn {
  name: string;
  type?: "text" | "number" | "severity" | "state" | "entity_ref" | "boolean" | "date";
}

export interface TableContent {
  title: string;
  columns: TableColumn[];
  rows: unknown[][];
}

export interface Badge {
  label: string;
  color?: "red" | "orange" | "yellow" | "blue" | "green" | "gray" | "purple";
}

export interface EntityField {
  label: string;
  value: string;
}

export interface EntityCardContent {
  entityType: string;
  entityId: string;
  displayId: string;
  title: string;
  severity?: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO" | null;
  state?: string | null;
  riskScore?: number | null;
  badges?: Badge[];
  fields?: EntityField[];
}

export interface Metric {
  label: string;
  value: number;
  format?: "number" | "percent" | "currency" | "duration";
  color?: "red" | "orange" | "yellow" | "blue" | "green" | "gray" | null;
}

export interface MetricRowContent {
  metrics: Metric[];
}

export interface ListItemContent {
  text: string;
  severity?: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO" | null;
}

export interface ListContent {
  title?: string;
  style?: "bulleted" | "numbered" | "checklist";
  items: ListItemContent[];
}

export interface ActionButton {
  label: string;
  action: string;
  params?: Record<string, unknown>;
  style?: "primary" | "secondary" | "danger";
}

export interface ActionButtonsContent {
  actions: ActionButton[];
}

export interface CodeContent {
  language?: "cypher" | "json" | "sql" | "yaml";
  title?: string;
  code: string;
}

export interface ErrorContent {
  severity?: "info" | "warning" | "error";
  title: string;
  body: string;
  recoveryHint?: string | null;
}

export interface SeparatorContent {
  label?: string | null;
}

export type BlockContent =
  | TextContent
  | TableContent
  | EntityCardContent
  | MetricRowContent
  | ListContent
  | ActionButtonsContent
  | CodeContent
  | ErrorContent
  | SeparatorContent;

export interface ContentBlock {
  type: BlockType;
  content: BlockContent;
}

export interface SourceRef {
  tool: string;
  description?: string;
  recordCount?: number;
}

export interface ResponseMeta {
  requestId: string;
  conversationId?: string | null;
  tokensUsed: number;
  costUsd: number;
  durationMs: number;
  modelUsed?: string;
}

export interface StructuredResponse {
  agent: string;
  responseType: string;
  confidence: number;
  status: "success" | "partial" | "error" | "no_data";
  blocks: ContentBlock[];
  payload?: Record<string, unknown> | null;
  thinking: string[];
  followUps: string[];
  sources: SourceRef[];
  meta: ResponseMeta;
}

// ── SSE stream events (conversation/sse.py) ────────────────────────────────

export type StepStatus = "pending" | "in_progress" | "completed" | "failed";

export interface StepEventData {
  step_id: string;
  node: string;
  status: StepStatus;
  message: string;
  detail?: Record<string, unknown> | null;
  ts: string;
  seq: number;
}

export type ChatStreamEvent =
  | { type: "token"; data: string }
  | { type: "step"; data: StepEventData }
  | { type: "response"; data: StructuredResponse }
  | { type: "ceiling"; data: { ceiling: ConversationCeiling } }
  | { type: "warning"; data: { level: string; ceiling: ConversationCeiling } }
  | { type: "meta"; data: { tokensThisTurn: number; ceiling?: ConversationCeiling } }
  | { type: "error"; data: string }
  | { type: "done" };

// ── Itinerary / HITL (gateway trips surface) ────────────────────────────────
//
// Mirrors services/gateway/src/temporal/itineraryMapper.ts's MappedItinerary
// exactly — that mapper is the only place the raw Python ItineraryResult
// (snake_case, no aliasing — see its own docstring) gets translated, so this
// type is a real contract, not an aspirational one. Fields the backend
// genuinely has no data for (a structured start/end time, a numeric
// confidence score, a named booking-provider distinct from the airline/
// hotel itself) are intentionally absent rather than fabricated.

export type PolicyDecision = "compliant" | "breach" | "waived" | "not_applicable" | "pending";

export interface ItinerarySegment {
  id: string;
  /** Which fetched candidate this segment came from — required to rate it
   * (POST /:planId/rate) or request a change to it. Null only for segments
   * compiled before this field existed. */
  candidateId: string | null;
  kind: "flight" | "hotel" | "attraction" | "dining" | string;
  title: string;
  subtitle: string | null;
  amount: number | null;
  currency: string;
  policyDecision: PolicyDecision;
  snapshotId: string | null;
  /** The specialist's own "why this ranks where it does" text
   * (providers/explain.py::annotate_reasons) — real, not placeholder. */
  reason: string | null;
  handoffLink: string | null;
  /** Which ranking arm produced this candidate — null means it predates
   * providers/ranking/ or personalization is off for this user. A segment
   * can only be rated when this is set (see SegmentTicket's rating control). */
  rankArm: "primitive" | "bandit" | null;
}

export interface ItineraryView {
  planId: string;
  status: string;
  version: number;
  totalAmount: number;
  currency: string;
  assumptions: string[];
  policyStatus: PolicyDecision;
  requiresApproval: boolean;
  segments: ItinerarySegment[];
}

export interface EditLogEntry {
  at: string;
  summary: string;
}
