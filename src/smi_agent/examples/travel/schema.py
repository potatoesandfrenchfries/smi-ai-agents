"""Travel domain schema — entity types, relationships, and labels.

Defines the graph data model for a travel management system:
flights, hotels, bookings, destinations, airlines, travelers.
"""

from smi_agent.domain.interfaces import DomainSchema


class TravelDomainSchema(DomainSchema):
    """Schema for the travel domain graph and entity model."""

    @property
    def entity_labels(self) -> list[str]:
        return [
            "Flight",
            "Hotel",
            "Booking",
            "Destination",
            "Airline",
            "Traveler",
            "Airport",
            "Itinerary",
            "Review",
            "PriceAlert",
        ]

    @property
    def relationship_types(self) -> list[str]:
        return [
            "DEPARTS_FROM",
            "ARRIVES_AT",
            "OPERATED_BY",
            "BOOKED_ON",
            "BELONGS_TO",
            "HAS_STOPOVER",
            "LOCATED_IN",
            "REVIEWED_BY",
            "PART_OF",
            "CONNECTS_TO",
            "HAS_ALTERNATIVE",
        ]

    @property
    def entity_types_for_context(self) -> list[str]:
        return ["flight", "hotel", "booking", "destination", "airline", "traveler", "itinerary", "dashboard"]

    @property
    def display_id_prefixes(self) -> dict[str, str]:
        return {
            "flight": "FLT",
            "hotel": "HTL",
            "booking": "BKG",
            "destination": "DST",
            "airline": "ALN",
            "traveler": "TRV",
            "itinerary": "ITN",
        }