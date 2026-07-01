"""Travel domain Cypher template catalog for graph queries.

Defines Neo4j graph templates for travel relationships:
flight routes, airline networks, hotel connections, booking graphs.
"""

from __future__ import annotations

from smi_agent.domain.interfaces import GraphQueryProvider


class TravelGraphQueryProvider(GraphQueryProvider):
    """Graph query provider for the travel domain."""

    @property
    def cypher_dir(self) -> str:
        return "examples/travel/cypher"

    @property
    def catalog(self) -> dict[str, dict]:
        return {
            "fetch_flight_routes": {
                "description": "Explore flight routes from an airport — direct flights, connections, airlines, and destinations",
                "parameters": {
                    "airport_id": {"type": "str", "required": True},
                    "tenant_id": {"type": "str", "required": True},
                    "max_hops": {"type": "int", "required": False, "default": 2, "interpolate": True},
                },
                "cost_class": "medium",
                "graph_touches": {"labels": ["Airport", "Flight", "Airline", "Destination"], "relationships": ["DEPARTS_FROM", "ARRIVES_AT", "OPERATED_BY"]},
            },
            "fetch_airline_network": {
                "description": "Explore an airline's route network — all airports served and interconnections",
                "parameters": {
                    "airline_id": {"type": "str", "required": True},
                    "tenant_id": {"type": "str", "required": True},
                },
                "cost_class": "medium",
                "graph_touches": {"labels": ["Airline", "Flight", "Airport", "Destination"], "relationships": ["OPERATED_BY", "DEPARTS_FROM", "ARRIVES_AT"]},
            },
            "fetch_booking_graph": {
                "description": "Explore related bookings — travelers, flights, hotels in a single itinerary graph",
                "parameters": {
                    "booking_id": {"type": "str", "required": True},
                    "tenant_id": {"type": "str", "required": True},
                },
                "cost_class": "cheap",
                "graph_touches": {"labels": ["Booking", "Flight", "Hotel", "Traveler"], "relationships": ["BOOKED_ON", "BELONGS_TO", "PART_OF"]},
            },
            "fetch_destination_network": {
                "description": "Explore all routes to a destination — which airlines fly there from which cities",
                "parameters": {
                    "destination_id": {"type": "str", "required": True},
                    "tenant_id": {"type": "str", "required": True},
                },
                "cost_class": "medium",
                "graph_touches": {"labels": ["Destination", "Airport", "Flight", "Airline"], "relationships": ["LOCATED_IN", "ARRIVES_AT", "OPERATED_BY"]},
            },
        }