"""Maps/places search tool — finds points of interest near a location.

Prototype implementation: attempts a real Nominatim (OpenStreetMap) fetch —
same "no API key" convention as hotel_scraper.py/restaurant_scraper.py's
Overpass calls, and the same OSM infra, just the geocoding/POI-search
endpoint instead of the query endpoint — falls back to seeded mock data on
any failure.
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from smi_agent.cache.redis_cache import fingerprint, get_or_set
from smi_agent.config.redis_keys import maps_cache_key

logger = logging.getLogger(__name__)

# Places move even less often than hotel listings — long TTL is safe.
_CACHE_TTL_SECONDS = 3600

_PLACE_TYPES = [
    "landmark", "museum", "park", "viewpoint", "monument", "square", "market",
]


def _seeded_places(location: str, query: str | None, num_results: int) -> list[dict[str, Any]]:
    """Generate deterministic mock place results seeded from location + query."""
    seed = int(hashlib.md5(f"{location}{query or ''}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    places = []
    for i in range(num_results):
        place_type = rng.choice(_PLACE_TYPES)
        places.append({
            "name": f"{location.title()} {place_type.title()} {i + 1}",
            "type": place_type,
            "lat": round(rng.uniform(-90, 90), 6),
            "lon": round(rng.uniform(-180, 180), 6),
            "address": f"{location.title()}",
        })
    return places


async def search_places(
    location: str, query: str | None = None, num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for points of interest at or near a location.

    Prototype: tries a lightweight HTTP scrape first; falls back to mock data.

    Args:
        location:    City name (e.g. "Edinburgh").
        query:       Optional place/POI type or keyword (e.g. "museum").
        num_results: Maximum number of results to return (default 5).

    Returns:
        List of place dicts, each with: name, type, lat, lon, address.
    """
    return await _fetch_places(location, query, num_results)


async def search_places_mock(
    location: str, query: str | None = None, num_results: int = 5,
) -> list[dict[str, Any]]:
    """Deterministic seeded results only — no network. For offline/test providers."""
    return _seeded_places(location, query, num_results)


async def _fetch_places(location: str, query: str | None, num_results: int) -> list[dict[str, Any]]:
    """Cached wrapper around the live fetch — reused across identical location+query searches."""
    key = maps_cache_key(fingerprint(location.lower(), (query or "").lower(), num_results))
    return await get_or_set(
        key, _CACHE_TTL_SECONDS, lambda: _fetch_places_live(location, query, num_results)
    )


async def _fetch_places_live(
    location: str, query: str | None, num_results: int,
) -> list[dict[str, Any]]:
    """Attempt real HTTP fetch; fall back to mock data on any failure."""
    try:
        import httpx  # optional dep — in 'conversation' extra

        search_term = f"{query} in {location}" if query else location
        # Overpass/OSM infra rejects requests with no (or a generic) User-Agent.
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Smartinerary/0.1"}) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": search_term, "format": "jsonv2", "limit": num_results},
            )
            if response.status_code == 200:
                data = response.json()
                if data:
                    return _normalise_nominatim(data)
    except Exception as exc:
        logger.debug("Maps HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_places(location, query, num_results)


def _normalise_nominatim(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Nominatim response fields to our internal place schema."""
    return [
        {
            "name": p.get("display_name", "").split(",")[0] or "Unknown",
            "type": p.get("type", "place"),
            "lat": float(p["lat"]) if p.get("lat") else None,
            "lon": float(p["lon"]) if p.get("lon") else None,
            "address": p.get("display_name", ""),
        }
        for p in raw
    ]
