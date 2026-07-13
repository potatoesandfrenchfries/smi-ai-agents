"""Flight providers — concrete implementations of FlightProvider.

To add a new flight data source: write a class with an async search()
matching FlightProvider's signature, then register it in
providers/registry.py. No other file needs to change.
"""

from __future__ import annotations

from typing import Any

from smi_agent.examples.travel.tools import flight_scraper


class AviationStackFlightProvider:
    """Live AviationStack fetch (Redis-cached), seeded mock fallback on failure."""

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        sort_by: str = "cost",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await flight_scraper.search_flights(
            origin=origin, destination=destination, date=date,
            sort_by=sort_by, num_results=num_results,
        )


class MockFlightProvider:
    """Deterministic seeded data only — no network. For offline dev/tests."""

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        sort_by: str = "cost",
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await flight_scraper.search_flights_mock(
            origin=origin, destination=destination, date=date,
            sort_by=sort_by, num_results=num_results,
        )
