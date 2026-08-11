"""Weather providers — concrete implementations of WeatherProvider.

To add a new weather data source: write a class with an async search()
matching WeatherProvider's signature, then register it in
providers/registry.py. No other file needs to change.
"""

from __future__ import annotations

from typing import Any

from smi_agent.examples.travel.tools import weather_scraper


class OpenMeteoWeatherProvider:
    """Live Open-Meteo fetch (Redis-cached), seeded mock fallback on failure."""

    async def search(
        self,
        location: str,
        date: str,
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await weather_scraper.search_weather(location=location, date=date, num_results=num_results)


class MockWeatherProvider:
    """Deterministic seeded data only — no network. For offline dev/tests."""

    async def search(
        self,
        location: str,
        date: str,
        num_results: int = 5,
    ) -> list[dict[str, Any]]:
        return await weather_scraper.search_weather_mock(
            location=location, date=date, num_results=num_results,
        )
