"""Maps providers — concrete implementations of MapsProvider.

To add a new maps/places data source: write a class with an async search()
matching MapsProvider's signature, then register it in
providers/registry.py. No other file needs to change.
"""

from __future__ import annotations

from typing import Any

from smi_agent.examples.travel.tools import maps_scraper


class NominatimMapsProvider:
    """Live OpenStreetMap Nominatim fetch (Redis-cached), seeded mock fallback on failure."""

    async def search(
        self,
        location: str,
        query: str | None = None,
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await maps_scraper.search_places(location=location, query=query, num_results=num_results)


class MockMapsProvider:
    """Deterministic seeded data only — no network. For offline dev/tests."""

    async def search(
        self,
        location: str,
        query: str | None = None,
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await maps_scraper.search_places_mock(
            location=location, query=query, num_results=num_results,
        )
