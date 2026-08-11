"""MCP server exposing Flight/Hotel/Restaurant/Weather/Maps/Budget search as
standardized MCP tools.

Every tool here is a thin wrapper around the existing providers/registry.py
factories — this module owns schema + transport only, not search logic, so
a new/replaced data source only ever needs a change in providers/, never
here (see providers/interface.py's own docstring for that contract).

Callers reach these tools over the network (Streamable HTTP) via
mcp_client/client.py, from both the `agents/` conversational path
(through ToolRegistry) and the Temporal/LangGraph itinerary path
(graph/itinerary_graph.py, activities/travel_activities.py) — see
src/smi_agent/README.md's "Ranking feedback loop"-style section for how
each path reaches this server.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="smi-agent-tools",
    description=(
        "Flight, hotel, restaurant, weather, maps, and budget search tools "
        "for the Smartinerary travel planning agents."
    ),
)


@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    date: str,
    sort_by: str = "cost",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for available flights between two airports on a date.

    Args:
        origin: IATA airport code or city name (e.g. "EDI", "Edinburgh").
        destination: IATA airport code or city name (e.g. "LHR", "London").
        date: Departure date in YYYY-MM-DD format.
        sort_by: Ranking preference — "cost", "comfort", or "time".
        num_results: Maximum number of results to return.
    """
    from smi_agent.providers.registry import get_flight_provider

    return await get_flight_provider().search(
        origin=origin, destination=destination, date=date,
        sort_by=sort_by, num_results=num_results,
    )


@mcp.tool()
async def search_hotels(
    location: str,
    check_in: str,
    check_out: str,
    sort_by: str = "rating",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for available hotels at a location for a date range.

    Args:
        location: City name (e.g. "Edinburgh").
        check_in: Check-in date in YYYY-MM-DD format.
        check_out: Check-out date in YYYY-MM-DD format.
        sort_by: Ranking preference — "price", "rating", or "proximity".
        num_results: Maximum number of results to return.
    """
    from smi_agent.providers.registry import get_hotel_provider

    return await get_hotel_provider().search(
        location=location, check_in=check_in, check_out=check_out,
        sort_by=sort_by, num_results=num_results,
    )


@mcp.tool()
async def search_restaurants(
    location: str,
    cuisine: str | None = None,
    sort_by: str = "rating",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for restaurants at a location.

    Args:
        location: City name (e.g. "Edinburgh").
        cuisine: Optional cuisine filter (e.g. "Italian").
        sort_by: Ranking preference — currently only "rating" is supported.
        num_results: Maximum number of results to return.
    """
    from smi_agent.providers.registry import get_restaurant_provider

    return await get_restaurant_provider().search(
        location=location, cuisine=cuisine, sort_by=sort_by, num_results=num_results,
    )


@mcp.tool()
async def get_weather(
    location: str,
    date: str,
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Get a daily weather forecast for a location starting on a date.

    Args:
        location: City name (e.g. "Edinburgh").
        date: Forecast start date in YYYY-MM-DD format.
        num_results: Number of consecutive days to return.
    """
    from smi_agent.providers.registry import get_weather_provider

    return await get_weather_provider().search(location=location, date=date, num_results=num_results)


@mcp.tool()
async def search_maps(
    location: str,
    query: str | None = None,
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for points of interest at or near a location.

    Args:
        location: City name (e.g. "Edinburgh").
        query: Optional place/POI type or keyword (e.g. "museum").
        num_results: Maximum number of results to return.
    """
    from smi_agent.providers.registry import get_maps_provider

    return await get_maps_provider().search(location=location, query=query, num_results=num_results)


@mcp.tool()
async def check_budget(
    flights: list[dict[str, Any]],
    hotels: list[dict[str, Any]],
    restaurants: list[dict[str, Any]],
    attractions: list[dict[str, Any]],
    current_total_gbp: float,
    budget_gbp: float | None = None,
    trip_type: str = "leisure",
    num_results: int = 3,
) -> list[dict[str, Any]]:
    """Suggest cheaper alternative flight/hotel/dining combos within budget.

    Re-ranks candidates already fetched by search_flights/search_hotels/
    search_restaurants/search_maps — issues no new searches itself.

    Args:
        flights: Flight candidates already fetched via search_flights.
        hotels: Hotel candidates already fetched via search_hotels.
        restaurants: Restaurant candidates already fetched via search_restaurants.
        attractions: Attraction candidates, if any (leisure trips only).
        current_total_gbp: The plan's current total cost.
        budget_gbp: The traveler's budget ceiling, if known.
        trip_type: "leisure" or "business" — affects whether attractions are dropped.
        num_results: Maximum number of alternative combos to return.
    """
    from smi_agent.providers.registry import get_budget_provider

    return await get_budget_provider().search(
        flights=flights, hotels=hotels, restaurants=restaurants, attractions=attractions,
        current_total_gbp=current_total_gbp, budget_gbp=budget_gbp,
        trip_type=trip_type, num_results=num_results,
    )
