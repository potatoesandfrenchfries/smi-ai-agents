"""Travel domain example — flights, hotels, bookings, destinations.

A complete domain implementation demonstrating all extension points
of the SMI Agent Seed framework.

Usage::

    # Set env var: SMI_DOMAIN=smi_agent.examples.travel
    # The framework auto-configures via DomainRegistry at startup.

    # Or manually:
    from smi_agent.domain.registry import DomainRegistry
    from smi_agent.examples.travel import domain

    DomainRegistry.configure(domain)

Entity model:
    - Flight: route, airline, status, price, aircraft
    - Hotel: star rating, amenities, location, pricing
    - Booking: traveler, flights/hotels, payment, seat class
    - Destination: city/country, airports, connections
    - Airline: fleet, route network, on-time performance

Graph relationships:
    - DEPARTS_FROM / ARRIVES_AT: Flight ↔ Airport
    - OPERATED_BY: Flight → Airline
    - BOOKED_ON: Booking → Flight/Hotel
    - BELONGS_TO: Booking → Traveler
    - CONNECTS_TO: Airport ↔ Airport (via flights)
"""

# Domain module — all attributes follow the DomainModule protocol.
# Set any to None to use the default fallback for that interface.
schema = None        # Imported below
query_provider = None
graph_query_provider = None
tool_mapping = None
entity_resolver = None
template_provider = None


def _lazy_import():
    """Lazy-import domain components to avoid circular imports at module level."""
    global schema, query_provider, graph_query_provider, tool_mapping, entity_resolver, template_provider

    if schema is None:
        from smi_agent.examples.travel.entity_resolver import TravelEntityResolver
        from smi_agent.examples.travel.graph_queries import TravelGraphQueryProvider
        from smi_agent.examples.travel.queries import TravelQueryProvider
        from smi_agent.examples.travel.schema import TravelDomainSchema
        from smi_agent.examples.travel.template_provider import TravelTemplateProvider
        from smi_agent.examples.travel.tool_mappings import TravelToolMapping

        schema = TravelDomainSchema()
        query_provider = TravelQueryProvider()
        graph_query_provider = TravelGraphQueryProvider()
        tool_mapping = TravelToolMapping()
        entity_resolver = TravelEntityResolver()
        template_provider = TravelTemplateProvider()


# Eager init: resolve all components at import time
_lazy_import()