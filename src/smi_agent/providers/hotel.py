"""Hotel providers — concrete implementations of HotelProvider.

To add a new hotel data source: write a class with an async search()
matching HotelProvider's signature, then register it in
providers/registry.py. No other file needs to change.
"""

from __future__ import annotations

from typing import Any

from smi_agent.examples.travel.tools import hotel_scraper


class OverpassHotelProvider:
    """Live OpenStreetMap Overpass fetch (Redis-cached), seeded mock fallback on failure."""

    async def search(
        self,
        location: str,
        check_in: str,
        check_out: str,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await hotel_scraper.search_hotels(
            location=location, check_in=check_in, check_out=check_out,
            sort_by=sort_by, num_results=num_results,
        )


class MockHotelProvider:
    """Deterministic seeded data only — no network. For offline dev/tests."""

    async def search(
        self,
        location: str,
        check_in: str,
        check_out: str,
        sort_by: str = "rating",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await hotel_scraper.search_hotels_mock(
            location=location, check_in=check_in, check_out=check_out,
            sort_by=sort_by, num_results=num_results,
        )
