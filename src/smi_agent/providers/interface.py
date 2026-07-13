"""Provider interfaces for Flights, Hotels, and Restaurants.

Every call site (Temporal activities, the itinerary LangGraph, specialist
agents) depends only on these Protocols, never on a concrete data source. A
new data source becomes a new class implementing the matching Protocol, plus
one entry in providers/registry.py — no changes anywhere else.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FlightProvider(Protocol):
    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        sort_by: str = "cost",
        num_results: int = 5,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class HotelProvider(Protocol):
    async def search(
        self,
        location: str,
        check_in: str,
        check_out: str,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class RestaurantProvider(Protocol):
    async def search(
        self,
        location: str,
        cuisine: str | None = None,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]: ...
