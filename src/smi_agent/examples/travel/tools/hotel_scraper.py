"""Hotel search tool — scrapes/queries for available hotels at a location.

Prototype implementation: attempts a real HTTP request to a public hotel data
source, falls back to seeded mock data for development.

Sort options:
    price      — cheapest nightly rate first
    rating     — highest guest review score first
    proximity  — closest to city centre first
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

HOTEL_NAMES = [
    "The Grand", "City Suites", "Harbour View", "Central Plaza",
    "The Riverside Inn", "Park Hotel", "Old Town Lodge", "Metro Boutique",
]
AMENITIES_POOL = [
    "Free WiFi", "Breakfast included", "Gym", "Pool", "Parking",
    "Bar", "Restaurant", "Spa", "Airport shuttle", "Pet-friendly",
]


def _seeded_hotels(location: str, check_in: str, check_out: str) -> list[dict[str, Any]]:
    """Generate deterministic mock hotel results seeded from location + dates."""
    seed = int(hashlib.md5(f"{location}{check_in}{check_out}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    nights = _nights(check_in, check_out)
    results = []
    for i in range(6):
        name = rng.choice(HOTEL_NAMES) + (f" {rng.choice(['&', 'and'])} Spa" if i % 3 == 0 else "")
        stars = rng.choice([3, 3, 4, 4, 4, 5])
        rating = round(rng.uniform(6.5, 9.8), 1)
        price_per_night = round(rng.uniform(60, 400) * (stars / 3), 2)
        distance_km = round(rng.uniform(0.2, 8.0), 1)
        amenities = rng.sample(AMENITIES_POOL, k=rng.randint(3, 6))
        results.append({
            "id": f"HTL-{seed % 10000:04d}-{i}",
            "name": name,
            "location": location,
            "stars": stars,
            "rating": rating,
            "review_count": rng.randint(50, 3000),
            "price_per_night_gbp": price_per_night,
            "total_price_gbp": round(price_per_night * nights, 2),
            "nights": nights,
            "distance_from_centre_km": distance_km,
            "amenities": amenities,
            "check_in": check_in,
            "check_out": check_out,
            "rooms_available": rng.randint(1, 20),
        })
    return results


def _nights(check_in: str, check_out: str) -> int:
    try:
        from datetime import date
        d1 = date.fromisoformat(check_in)
        d2 = date.fromisoformat(check_out)
        return max(1, (d2 - d1).days)
    except Exception:
        return 1


async def search_hotels(
    location: str,
    check_in: str,
    check_out: str,
    sort_by: str = "rating",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for available hotels at a location.

    Prototype: tries a lightweight HTTP request first; falls back to mock data.

    Args:
        location:    City, area, or landmark (e.g. "Paris", "Edinburgh city centre").
        check_in:    Check-in date in YYYY-MM-DD format.
        check_out:   Check-out date in YYYY-MM-DD format.
        sort_by:     Ranking preference — "price", "rating", or "proximity".
        num_results: Maximum number of results to return (default 5).

    Returns:
        List of hotel dicts, each with: id, name, stars, rating, price_per_night_gbp,
        total_price_gbp, nights, distance_from_centre_km, amenities.
    """
    hotels = await _fetch_hotels(location, check_in, check_out)
    hotels = _sort(hotels, sort_by)
    return hotels[:num_results]


async def _fetch_hotels(location: str, check_in: str, check_out: str) -> list[dict[str, Any]]:
    """Attempt real HTTP fetch; fall back to mock data on any failure."""
    try:
        import httpx

        # Prototype target: Open-Meteo / Nominatim for geocoding to demonstrate
        # the HTTP pattern. Replace with a hotel API (e.g. Booking.com affiliate,
        # RapidAPI Hotels) in production.
        url = (
            f"https://nominatim.openstreetmap.org/search"
            f"?q={location}&format=json&limit=1"
        )
        async with httpx.AsyncClient(timeout=5.0, headers={"User-Agent": "Smartinerary/0.1"}) as client:
            response = await client.get(url)
            if response.status_code == 200 and response.json():
                # Geocoding worked — in production we'd pass lat/lon to a hotel API.
                # For prototype, use the confirmed location name with mock data.
                geo = response.json()[0]
                logger.debug("Hotel geo lookup OK: %s", geo.get("display_name"))
    except Exception as exc:
        logger.debug("Hotel HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_hotels(location, check_in, check_out)


def _sort(hotels: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    if sort_by == "price":
        return sorted(hotels, key=lambda h: h["price_per_night_gbp"])
    if sort_by == "rating":
        return sorted(hotels, key=lambda h: h["rating"], reverse=True)
    if sort_by == "proximity":
        return sorted(hotels, key=lambda h: h["distance_from_centre_km"])
    return hotels


SEARCH_HOTELS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "search_hotels",
        "description": (
            "Search for available hotels at a location for given dates. "
            "Returns a ranked list of options with price, rating, and amenities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City, area, or landmark (e.g. 'Paris', 'Edinburgh city centre')",
                },
                "check_in": {
                    "type": "string",
                    "description": "Check-in date in YYYY-MM-DD format",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date in YYYY-MM-DD format",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["price", "rating", "proximity"],
                    "description": "Ranking preference. Default is 'rating'.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5)",
                },
            },
            "required": ["location", "check_in", "check_out"],
        },
    },
}
