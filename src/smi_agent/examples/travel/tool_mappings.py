"""Travel domain tool mappings — response types, capability keywords, domain anchors."""

from smi_agent.domain.interfaces import ToolMappingProvider


class TravelToolMapping(ToolMappingProvider):
    """Maps travel domain tools to response types and capability keywords."""

    def tool_to_response_type(self, tool_name: str) -> str | None:
        mapping = {
            # Graph tools
            "fetch_flight_routes": "relationship_graph",
            "fetch_airline_network": "relationship_graph",
            "fetch_booking_graph": "relationship_graph",
            "fetch_destination_network": "relationship_graph",
            # Flight tools
            "fetch_top_flights": "entity_list",
            "fetch_flight_stats": "metrics_overview",
            "fetch_flight_detail": "entity_detail",
            # Hotel tools
            "fetch_top_hotels": "entity_list",
            "fetch_hotel_detail": "entity_detail",
            # Booking tools
            "fetch_booking_detail": "entity_detail",
            "fetch_booking_stats": "metrics_overview",
        }
        return mapping.get(tool_name)

    def tool_capability_keywords(self, tool_name: str) -> list[str]:
        keywords = {
            "fetch_flight_routes": [
                "route", "routes", "flight", "flights", "airport", "airports",
                "connection", "connections", "direct", "stopover", "connecting",
                "depart", "arrive", "departure", "arrival",
            ],
            "fetch_airline_network": [
                "airline", "airlines", "network", "fleet", "destinations",
                "operated by", "carrier", "hub",
            ],
            "fetch_booking_graph": [
                "booking", "bookings", "itinerary", "trip", "reservation",
                "traveler", "passenger", "guest",
            ],
            "fetch_destination_network": [
                "destination", "destinations", "city", "country", "travel to",
                "visit", "tourist", "popular",
            ],
            "fetch_flight_detail": [
                "flight detail", "flight info", "tell me about flight",
                "flight status", "aircraft", "seat", "gate",
            ],
            "fetch_top_flights": [
                "upcoming flights", "next flight", "today flight",
                "available flights", "cheapest flight", "earliest flight",
            ],
            "fetch_flight_stats": [
                "flight statistics", "how many flights", "flight count",
                "flight overview", "% delayed", "on time",
            ],
            "fetch_hotel_detail": [
                "hotel detail", "hotel info", "tell me about hotel",
                "amenities", "star rating", "pool", "gym", "wifi",
            ],
            "fetch_top_hotels": [
                "top hotels", "best hotels", "recommended hotels",
                "highest rated", "popular hotels", "cheapest hotels",
            ],
            "fetch_booking_detail": [
                "booking detail", "booking info", "reservation detail",
                "my booking", "booking status", "cancellation policy",
                "refund", "payment",
            ],
            "fetch_booking_stats": [
                "booking statistics", "how many bookings", "booking count",
                "revenue", "total revenue", "booking overview",
                "booking summary",
            ],
        }
        return keywords.get(tool_name, [])

    @property
    def domain_anchors(self) -> set[str]:
        return {
            "flight", "flights", "airline", "airlines", "airport", "airports",
            "hotel", "hotels", "booking", "bookings", "reservation",
            "destination", "city", "travel", "trip", "itinerary",
            "route", "passenger", "traveler", "departure", "arrival",
            "price", "fare", "seat", "gate", "terminal",
            "rating", "review", "amenity", "check-in", "check-out",
            "cancel", "refund", "baggage", "meal",
        }

    @property
    def universal_keywords(self) -> list[str]:
        return [
            "who", "what", "which", "how many", "show", "list",
            "find", "describe", "explain", "compare", "top", "most",
            "cheapest", "best", "worst", "next", "upcoming", "recent",
        ]