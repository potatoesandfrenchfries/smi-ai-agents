"""Domain abstraction layer — plug in your domain via 6 interfaces.

Usage::

    from smi_agent.domain.registry import DomainRegistry
    from smi_agent.examples.travel import domain as travel_domain

    DomainRegistry.configure(travel_domain)
"""

from smi_agent.domain.interfaces import (
    DomainModule,
    DomainSchema,
    EntityResolver,
    GraphQueryProvider,
    QueryProvider,
    TemplateProvider,
    ToolMappingProvider,
)
from smi_agent.domain.registry import DomainRegistry

__all__ = [
    "DomainRegistry",
    "DomainSchema",
    "QueryProvider",
    "GraphQueryProvider",
    "ToolMappingProvider",
    "EntityResolver",
    "TemplateProvider",
    "DomainModule",
]