"""Structured response schema for the multi-agent chat system.

Every agent response uses the canonical ``StructuredResponse`` envelope.
The ``blocks`` array contains typed content blocks that the frontend
renders with purpose-built UI components.

Block types:
  text, table, entity_card, graph, metric_row, list,
  action_buttons, code, error, separator
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Block content models ──────────────────────────────────────────────────────


class Highlight(BaseModel):
    """Inline entity reference within text."""

    term: str
    entityType: str  # Domain-specific entity type (e.g., flight, hotel, booking)
    entityId: str


class TextContent(BaseModel):
    body: str
    highlights: list[Highlight] = Field(default_factory=list)


class TableColumn(BaseModel):
    name: str
    type: Literal["text", "number", "severity", "state", "entity_ref", "boolean", "date"] = "text"


class TableContent(BaseModel):
    title: str
    columns: list[TableColumn]
    rows: list[list[Any]]  # Each row matches columns; entity_ref format: "id|name|type"


class Badge(BaseModel):
    label: str
    color: Literal["red", "orange", "yellow", "blue", "green", "gray", "purple"] = "gray"


class EntityField(BaseModel):
    label: str
    value: str


class EntityCardContent(BaseModel):
    entityType: str  # Domain-specific: flight, hotel, booking, etc.
    entityId: str
    displayId: str
    title: str
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] | None = None
    state: str | None = None
    riskScore: float | None = None
    badges: list[Badge] = Field(default_factory=list)
    fields: list[EntityField] = Field(default_factory=list)


class GraphNodeProperty(BaseModel):
    label: str
    value: str


class GraphNode(BaseModel):
    id: str
    label: str
    nodeType: Literal["ORIGIN", "INTERMEDIATE", "TARGET", "ENTITY"] = "ENTITY"
    subtype: str | None = None
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] | None = None
    properties: list[GraphNodeProperty] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str = Field(alias="from")  # "from" is reserved in Python
    target: str = Field(alias="to")
    label: str = ""
    relationshipType: str | None = None
    evidence: str | None = None

    class Config:
        populate_by_name = True


class GraphContent(BaseModel):
    title: str
    layout: Literal["hierarchical", "force", "radial"] = "hierarchical"
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class Metric(BaseModel):
    label: str
    value: int | float
    format: Literal["number", "percent", "currency", "duration"] = "number"
    color: Literal["red", "orange", "yellow", "blue", "green", "gray"] | None = None


class MetricRowContent(BaseModel):
    metrics: list[Metric]


class ListItem(BaseModel):
    text: str
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] | None = None


class ListContent(BaseModel):
    title: str = ""
    style: Literal["bulleted", "numbered", "checklist"] = "bulleted"
    items: list[ListItem]


class ActionButton(BaseModel):
    label: str
    action: str  # navigate | create | accept | assign | copy
    params: dict[str, Any] = Field(default_factory=dict)
    style: Literal["primary", "secondary", "danger"] = "secondary"


class ActionButtonsContent(BaseModel):
    actions: list[ActionButton]


class CodeContent(BaseModel):
    language: Literal["cypher", "json", "sql", "yaml"] = "json"
    title: str = ""
    code: str


class ErrorContent(BaseModel):
    severity: Literal["info", "warning", "error"] = "warning"
    title: str
    body: str
    recoveryHint: str | None = None


class SeparatorContent(BaseModel):
    label: str | None = None


# ── Content block discriminated union ─────────────────────────────────────────

BlockType = Literal[
    "text", "table", "entity_card", "graph", "metric_row",
    "list", "action_buttons", "code", "error", "separator",
]


class ContentBlock(BaseModel):
    """A single renderable block in the response."""

    type: BlockType
    content: (
        TextContent
        | TableContent
        | EntityCardContent
        | GraphContent
        | MetricRowContent
        | ListContent
        | ActionButtonsContent
        | CodeContent
        | ErrorContent
        | SeparatorContent
    )


# ── Envelope models ──────────────────────────────────────────────────────────


class SourceRef(BaseModel):
    """Which tool/query produced data for this response."""

    tool: str
    description: str = ""
    recordCount: int = 0


class ResponseMeta(BaseModel):
    """Debug + audit metadata (not rendered to user)."""

    requestId: str = Field(default_factory=lambda: uuid.uuid4().hex)
    conversationId: str | None = None
    userId: str | None = None
    tenantId: str | None = None
    toolsUsed: list[str] = Field(default_factory=list)
    iterations: int = 0
    tokensUsed: int = 0
    costUsd: float = 0.0
    durationMs: int = 0
    agentVersion: str = ""
    modelUsed: str = ""


# ── ResponseType is domain-agnostic ───────────────────────────────────────────


ResponseType = Literal[
    "entity_list",        # A collection of domain entities (e.g., "Top Flights")
    "entity_detail",      # Single entity detail view (e.g., specific flight)
    "relationship_graph", # Graph visualization of relationships (e.g., flight routes)
    "metrics_overview",   # Aggregate metrics/stats (e.g., booking counts by status)
    "entity_lookup",      # Lookup a specific entity by ID
    "comparison",         # Compare two or more entities
    "greeting",           # Greeting or help message
    "error",              # Error response
    "text_only",          # Fallback: plain text response
]


class StructuredResponse(BaseModel):
    """Canonical response envelope — same shape for every agent."""

    agent: str = "supervisor"
    responseType: ResponseType = "text_only"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: Literal["success", "partial", "error", "no_data"] = "success"

    blocks: list[ContentBlock] = Field(default_factory=list)
    payload: dict[str, Any] | None = None
    thinking: list[str] = Field(default_factory=list)
    followUps: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    meta: ResponseMeta = Field(default_factory=ResponseMeta)


# ── Helpers ───────────────────────────────────────────────────────────────────


def text_block(body: str, **kwargs: Any) -> ContentBlock:
    """Quick helper to create a text block."""
    return ContentBlock(type="text", content=TextContent(body=body, **kwargs))


def error_block(title: str, body: str, severity: str = "warning", **kwargs: Any) -> ContentBlock:
    """Quick helper to create an error block."""
    return ContentBlock(type="error", content=ErrorContent(title=title, body=body, severity=severity, **kwargs))


def simple_response(agent: str, body: str, **kwargs: Any) -> StructuredResponse:
    """Create a simple text-only response."""
    return StructuredResponse(agent=agent, blocks=[text_block(body)], **kwargs)