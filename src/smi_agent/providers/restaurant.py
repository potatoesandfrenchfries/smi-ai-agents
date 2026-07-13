"""Restaurant providers — concrete implementations of RestaurantProvider.

To add a new restaurant data source: write a class with an async search()
matching RestaurantProvider's signature, then register it in
providers/registry.py. No other file needs to change.
"""

from __future__ import annotations

from typing import Any

from smi_agent.examples.travel.tools import restaurant_scraper


class OverpassRestaurantProvider:
    """Live OpenStreetMap Overpass fetch (Redis-cached), seeded mock fallback on failure."""

    async def search(
        self,
        location: str,
        cuisine: str | None = None,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await restaurant_scraper.search_restaurants(
            location=location, cuisine=cuisine, sort_by=sort_by, num_results=num_results,
        )


class MockRestaurantProvider:
    """Deterministic seeded data only — no network. For offline dev/tests."""

    async def search(
        self,
        location: str,
        cuisine: str | None = None,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await restaurant_scraper.search_restaurants_mock(
            location=location, cuisine=cuisine, sort_by=sort_by, num_results=num_results,
        )
