"""Abstract domain interfaces.

Implement these six interfaces to plug your domain into the agent framework.
See ``smi_agent.examples.travel`` for a complete working example.

Domain lifecycle
----------------
1. Implement the interfaces for your domain
2. Register via ``DomainRegistry.configure(domain_module)`` at startup
3. The framework resolves queries, templates, tool mappings, and routing
   through your implementations at runtime
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

# ── DomainSchema ─────────────────────────────────────────────────────────────────


class DomainSchema(ABC):
    """Entity types, relationships, and labels that define a domain's data model.

    Used by Neo4j schema validation, graph policy defaults, and the agent's
    system prompts to understand what entities exist.
    """

    @property
    @abstractmethod
    def entity_labels(self) -> list[str]:
        """Node labels in the graph database (e.g. ``["Flight", "Hotel", "Booking"]``)."""
        ...

    @property
    @abstractmethod
    def relationship_types(self) -> list[str]:
        """Relationship types in the graph database (e.g. ``["BOOKED_BY", "DEPARTS_FROM"]``)."""
        ...

    @property
    @abstractmethod
    def entity_types_for_context(self) -> list[str]:
        """Entity types used in page context routing.

        These are the values a frontend sends as ``context.type`` to indicate
        which kind of entity page the user is viewing.
        """
        ...

    @property
    @abstractmethod
    def display_id_prefixes(self) -> dict[str, str]:
        """Map entity type → display ID prefix (e.g. ``{"flight": "FLT", "booking": "BKG"}``)."""
        ...


# ── QueryProvider ────────────────────────────────────────────────────────────────


class QueryProvider(ABC):
    """Provides domain-specific Postgres/SQL queries and their OpenAI tool definitions.

    The framework calls these methods to build the tool registry for specialist agents.
    Read queries become tools the LLM can call; write queries are used by workflows.
    """

    @property
    @abstractmethod
    def read_queries(self) -> dict[str, Any]:
        """Dict of read query name → SqlQuery (or dict with name, sql, is_read)."""
        ...

    @property
    @abstractmethod
    def write_queries(self) -> dict[str, Any]:
        """Dict of write query name → SqlQuery."""
        ...

    @abstractmethod
    def tool_definitions(self) -> dict[str, dict[str, Any]]:
        """OpenAI function-calling tool definitions for domain read queries.

        Returns:
            Dict mapping query_name → {description, parameters: {properties, required}}.
            These are merged into the ToolRegistry's _PG_TOOL_DEFS map.
        """
        ...


# ── GraphQueryProvider ───────────────────────────────────────────────────────────


class GraphQueryProvider(ABC):
    """Provides domain-specific Cypher templates for Neo4j graph traversal.

    The framework loads these at startup via TemplateLoader and exposes them
    as tools when the agent's ``allowed_cypher_templates`` list includes them.
    """

    @property
    @abstractmethod
    def cypher_dir(self) -> str:
        """Path to the directory containing .cypher + .meta.yaml template pairs.

        Return a path relative to the project root (e.g. ``"cypher"`` or
        ``"examples/travel/cypher"``).
        """
        ...

    @property
    @abstractmethod
    def catalog(self) -> dict[str, dict[str, Any]]:
        """Template catalog: name → {description, parameters, allowed_agents, cost_class, ...}.

        Used for discovery and documentation. The actual loading is done by
        ``TemplateLoader`` from the ``cypher_dir``.
        """
        ...


# ── ToolMappingProvider ──────────────────────────────────────────────────────────


class ToolMappingProvider(ABC):
    """Maps tool names to response types, capability keywords, and domain anchors.

    The agent runtime uses these to:
    - Deterministically set ``responseType`` based on which tools were called
    - Validate follow-up questions against available tool capabilities
    - Anchor generic queries to this domain
    """

    @abstractmethod
    def tool_to_response_type(self, tool_name: str) -> str | None:
        """Return the response type for a tool call, or None if not domain-specific.

        Called for every tool used during a ReAct loop. The first recognized
        tool determines the ``responseType`` in the ``StructuredResponse``.
        """
        ...

    @abstractmethod
    def tool_capability_keywords(self, tool_name: str) -> list[str]:
        """Return keyword list describing what this tool can answer about.

        Used to validate follow-up questions: a question is answerable if
        it contains at least one keyword from at least one available tool.
        """
        ...

    @property
    @abstractmethod
    def domain_anchors(self) -> set[str]:
        """Keywords that anchor a generic query to this domain.

        Follow-up questions containing a universal keyword (who, what, list, etc.)
        AND a domain anchor keyword pass validation even if no tool keyword matches.
        """
        ...

    @property
    @abstractmethod
    def universal_keywords(self) -> list[str]:
        """Keywords that work for any domain (e.g. 'who', 'what', 'list', 'show')."""
        ...


# ── EntityResolver ───────────────────────────────────────────────────────────────


class EntityResolver(ABC):
    """Resolves page context to agent names and entity details.

    When a user opens a page (e.g. a flight detail view), the frontend sends
    a ``context`` with type + entityId. The resolver maps this to the correct
    agent definition and provides any entity-specific context.
    """

    @abstractmethod
    def resolve_agent_name(
        self,
        context_type: str | None = None,
        entity_id: str | None = None,
        label: str | None = None,
    ) -> str:
        """Return the agent definition name for a given page context.

        Args:
            context_type: Page type (e.g. "flight", "hotel", "booking").
            entity_id: Optional entity UUID.
            label: Optional display label (e.g. "Top Destinations").

        Returns:
            Agent definition name (without .yaml extension) to use for this page.
        """
        ...

    @abstractmethod
    def entity_id_required(self, context_type: str) -> bool:
        """Return True if entityId is required for this context type."""
        ...

    @abstractmethod
    def valid_page_types(self) -> set[str]:
        """Return the set of valid page context types for this domain."""
        ...


# ── TemplateProvider ─────────────────────────────────────────────────────────────


class TemplateProvider(ABC):
    """Generates AI insights template content for entity context pages.

    When a user opens a detail page, the frontend requests an AI-generated
    insights template. This provider fetches data and generates a structured
    summary with suggestions, charts data, and context chips.
    """

    @abstractmethod
    async def generate_template(
        self,
        pg_executor: Any,
        conversation_id: str,
        context_type: str | None,
        entity_id: str | None,
        label: str | None,
        tenant_id: str,
        on_step: Any = None,
    ) -> dict[str, Any]:
        """Generate template content for an entity context page.

        Args:
            pg_executor: SafePostgresExecutor for running domain queries.
            conversation_id: Conversation to attach the template to.
            context_type: Page type (e.g. "flight", "hotel").
            entity_id: Optional entity UUID.
            label: Optional display label (e.g. "Top Destinations").
            tenant_id: Tenant UUID.
            on_step: Optional async callback(str) for SSE progress events.

        Returns:
            Dict with: summary, recommendation, synthesisBullets,
            contextChips, suggestions, inputPlaceholder.
        """
        ...


# ── DomainModule protocol ────────────────────────────────────────────────────────


class DomainModule(Protocol):
    """Protocol for a domain module passed to ``DomainRegistry.configure()``.

    A domain module must expose these attributes. Each is optional — the
    registry falls back to sensible defaults for any that are missing.
    """

    schema: DomainSchema | None
    query_provider: QueryProvider | None
    graph_query_provider: GraphQueryProvider | None
    tool_mapping: ToolMappingProvider | None
    entity_resolver: EntityResolver | None
    template_provider: TemplateProvider | None