"""Weather forecast tool — queries a forecast for a location and date.

Prototype implementation: attempts a real Open-Meteo fetch (free, no key
required — geocoding + forecast, same "no API key" convention as
hotel_scraper.py/restaurant_scraper.py's Overpass calls), falls back to
seeded mock data on any failure.
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta
from typing import Any

from smi_agent.cache.redis_cache import fingerprint, get_or_set
from smi_agent.config.redis_keys import weather_cache_key

logger = logging.getLogger(__name__)

# Forecasts change more often than hotel listings but a short TTL still cuts
# repeat geocode+forecast calls for the same location+date within a session.
_CACHE_TTL_SECONDS = 900

_CONDITIONS = [
    "Clear", "Partly cloudy", "Cloudy", "Light rain", "Rain", "Thunderstorm", "Snow",
]

# WMO weather codes (Open-Meteo's forecast field) collapsed to the coarse
# labels above — https://open-meteo.com/en/docs#weathervariables
_WMO_TO_LABEL: dict[int, str] = {
    0: "Clear", 1: "Clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Cloudy", 48: "Cloudy",
    51: "Light rain", 53: "Light rain", 55: "Rain",
    61: "Light rain", 63: "Rain", 65: "Rain",
    71: "Snow", 73: "Snow", 75: "Snow",
    80: "Light rain", 81: "Rain", 82: "Rain",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


def _seeded_forecast(location: str, date: str, num_results: int) -> list[dict[str, Any]]:
    """Generate deterministic mock daily forecasts seeded from location + date."""
    seed = int(hashlib.md5(f"{location}{date}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    try:
        start = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        start = datetime.utcnow()

    forecasts = []
    for i in range(num_results):
        day = start + timedelta(days=i)
        temp_max = round(rng.uniform(10, 30), 1)
        temp_min = round(temp_max - rng.uniform(3, 10), 1)
        forecasts.append({
            "location": location,
            "date": day.strftime("%Y-%m-%d"),
            "condition": rng.choice(_CONDITIONS),
            "temp_max_c": temp_max,
            "temp_min_c": temp_min,
            "precipitation_probability_pct": rng.randint(0, 100),
        })
    return forecasts


async def search_weather(
    location: str, date: str, num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for a daily forecast starting at a location and date.

    Prototype: tries a lightweight HTTP scrape first; falls back to mock data.

    Args:
        location:    City name (e.g. "Edinburgh").
        date:        Forecast start date in YYYY-MM-DD format.
        num_results: Number of consecutive days to return (default 5).

    Returns:
        List of forecast dicts, each with: location, date, condition,
        temp_max_c, temp_min_c, precipitation_probability_pct.
    """
    return await _fetch_forecast(location, date, num_results)


async def search_weather_mock(
    location: str, date: str, num_results: int = 5,
) -> list[dict[str, Any]]:
    """Deterministic seeded results only — no network. For offline/test providers."""
    return _seeded_forecast(location, date, num_results)


async def _fetch_forecast(location: str, date: str, num_results: int) -> list[dict[str, Any]]:
    """Cached wrapper around the live fetch — reused across identical location+date searches."""
    key = weather_cache_key(fingerprint(location.lower(), date, num_results))
    return await get_or_set(
        key, _CACHE_TTL_SECONDS, lambda: _fetch_forecast_live(location, date, num_results)
    )


async def _fetch_forecast_live(
    location: str, date: str, num_results: int,
) -> list[dict[str, Any]]:
    """Attempt real HTTP fetch (geocode then forecast); fall back to mock data on any failure."""
    try:
        import httpx  # optional dep — in 'conversation' extra

        async with httpx.AsyncClient(timeout=5.0) as client:
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1},
            )
            if geo_resp.status_code != 200:
                raise ValueError(f"geocoding returned {geo_resp.status_code}")
            geo_results = geo_resp.json().get("results") or []
            if not geo_results:
                raise ValueError(f"no geocoding match for {location!r}")
            lat, lon = geo_results[0]["latitude"], geo_results[0]["longitude"]

            forecast_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "start_date": date,
                    "end_date": (
                        datetime.strptime(date, "%Y-%m-%d") + timedelta(days=num_results - 1)
                    ).strftime("%Y-%m-%d"),
                    "timezone": "auto",
                },
            )
            if forecast_resp.status_code == 200:
                return _normalise_open_meteo(location, forecast_resp.json())
    except Exception as exc:
        logger.debug("Weather HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_forecast(location, date, num_results)


def _normalise_open_meteo(location: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Map Open-Meteo's daily-arrays response to our internal forecast schema."""
    daily = raw.get("daily") or {}
    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    temp_max = daily.get("temperature_2m_max") or []
    temp_min = daily.get("temperature_2m_min") or []
    precip = daily.get("precipitation_probability_max") or []

    results = []
    for i, day in enumerate(dates):
        results.append({
            "location": location,
            "date": day,
            "condition": _WMO_TO_LABEL.get(codes[i] if i < len(codes) else -1, "Unknown"),
            "temp_max_c": temp_max[i] if i < len(temp_max) else None,
            "temp_min_c": temp_min[i] if i < len(temp_min) else None,
            "precipitation_probability_pct": precip[i] if i < len(precip) else None,
        })
    return results
