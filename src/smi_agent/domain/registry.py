"""DomainRegistry — pluggable domain configuration for the agent framework.

Usage::

    from smi_agent.domain.registry import DomainRegistry
    from smi_agent.examples.travel import domain as travel_domain

    # At app startup:
    DomainRegistry.configure(travel_domain)

    # Later, anywhere in the framework:
    entity_labels = DomainRegistry.schema().entity_labels
    tool_defs = DomainRegistry.query_provider().tool_definitions()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from smi_agent.domain.interfaces import (
        DomainModule,
        DomainSchema,
        EntityResolver,
        GraphQueryProvider,
        QueryProvider,
        TemplateProvider,
        ToolMappingProvider,
    )

logger = logging.getLogger(__name__)


# ── Default (no-op) implementations ──────────────────────────────────────────────


class _DefaultSchema:
    """Fallback schema with minimal generic labels."""

    entity_labels: list[str] = ["Entity", "Relationship"]
    relationship_types: list[str] = ["RELATES_TO"]
    entity_types_for_context: list[str] = ["general"]
    display_id_prefixes: dict[str, str] = {}


class _DefaultQueryProvider:
    """Fallback query provider — returns empty registries."""

    @property
    def read_queries(self) -> dict[str, Any]:
        return {}

    @property
    def write_queries(self) -> dict[str, Any]:
        return {}

    def tool_definitions(self) -> dict[str, dict[str, Any]]:
        return {}


class _DefaultGraphQueryProvider:
    """Fallback graph query provider — returns default cypher dir, empty catalog."""

    @property
    def cypher_dir(self) -> str:
        return "cypher"

    @property
    def catalog(self) -> dict[str, dict[str, Any]]:
        return {}


class _DefaultToolMapping:
    """Fallback tool mapping — no domain-specific mappings."""

    def tool_to_response_type(self, tool_name: str) -> str | None:
        return None

    def tool_capability_keywords(self, tool_name: str) -> list[str]:
        return []

    @property
    def domain_anchors(self) -> set[str]:
        return set()

    @property
    def universal_keywords(self) -> list[str]:
        return ["who", "what", "which", "how many", "show", "list",
                "find", "describe", "explain", "compare", "top", "most"]


class _DefaultEntityResolver:
    """Fallback entity resolver — uses 'conversation' agent for everything."""

    def resolve_agent_name(
        self,
        context_type: str | None = None,
        entity_id: str | None = None,
        label: str | None = None,
    ) -> str:
        return "conversation"

    def entity_id_required(self, context_type: str) -> bool:
        return False

    def valid_page_types(self) -> set[str]:
        return {"general", "dashboard"}


class _DefaultTemplateProvider:
    """Fallback template provider — returns empty template."""

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
        return {
            "summary": "",
            "recommendation": "",
            "synthesisBullets": [],
            "contextChips": [],
            "suggestions": [],
            "inputPlaceholder": f"Ask about {label or 'this page'}...",
        }


# ── Registry ─────────────────────────────────────────────────────────────────────


class DomainRegistry:
    """Singleton registry that holds the current domain's implementations.

    Call ``DomainRegistry.configure(module)`` once at startup. After that,
    all framework components resolve domain behavior through this registry.
    """

    _schema: DomainSchema = _DefaultSchema()
    _query_provider: QueryProvider = _DefaultQueryProvider()
    _graph_query_provider: GraphQueryProvider = _DefaultGraphQueryProvider()
    _tool_mapping: ToolMappingProvider = _DefaultToolMapping()
    _entity_resolver: EntityResolver = _DefaultEntityResolver()
    _template_provider: TemplateProvider = _DefaultTemplateProvider()
    _configured: bool = False

    @classmethod
    def configure(cls, domain_module: DomainModule) -> None:
        """Configure the domain from a module implementing the DomainModule protocol.

        Any attribute that is None keeps the default fallback implementation.
        """
        if getattr(domain_module, "schema", None) is not None:
            cls._schema = domain_module.schema
        if getattr(domain_module, "query_provider", None) is not None:
            cls._query_provider = domain_module.query_provider
        if getattr(domain_module, "graph_query_provider", None) is not None:
            cls._graph_query_provider = domain_module.graph_query_provider
        if getattr(domain_module, "tool_mapping", None) is not None:
            cls._tool_mapping = domain_module.tool_mapping
        if getattr(domain_module, "entity_resolver", None) is not None:
            cls._entity_resolver = domain_module.entity_resolver
        if getattr(domain_module, "template_provider", None) is not None:
            cls._template_provider = domain_module.template_provider

        cls._configured = True
        logger.info(
            "DomainRegistry configured: schema=%s query=%s graph=%s mapping=%s resolver=%s template=%s",
            type(cls._schema).__name__,
            type(cls._query_provider).__name__,
            type(cls._graph_query_provider).__name__,
            type(cls._tool_mapping).__name__,
            type(cls._entity_resolver).__name__,
            type(cls._template_provider).__name__,
        )

    @classmethod
    def schema(cls) -> DomainSchema:
        return cls._schema

    @classmethod
    def query_provider(cls) -> QueryProvider:
        return cls._query_provider

    @classmethod
    def graph_query_provider(cls) -> GraphQueryProvider:
        return cls._graph_query_provider

    @classmethod
    def tool_mapping(cls) -> ToolMappingProvider:
        return cls._tool_mapping

    @classmethod
    def entity_resolver(cls) -> EntityResolver:
        return cls._entity_resolver

    @classmethod
    def template_provider(cls) -> TemplateProvider:
        return cls._template_provider

    @classmethod
    def is_configured(cls) -> bool:
        return cls._configured

    @classmethod
    def reset(cls) -> None:
        """Reset to defaults (useful for testing)."""
        cls._schema = _DefaultSchema()
        cls._query_provider = _DefaultQueryProvider()
        cls._graph_query_provider = _DefaultGraphQueryProvider()
        cls._tool_mapping = _DefaultToolMapping()
        cls._entity_resolver = _DefaultEntityResolver()
        cls._template_provider = _DefaultTemplateProvider()
        cls._configured = False