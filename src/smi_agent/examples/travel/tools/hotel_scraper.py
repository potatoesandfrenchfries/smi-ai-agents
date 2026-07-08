"""Hotel search tool — scrapes/queries for available hotels at a location.

Prototype implementation: attempts a real Overpass API query (OpenStreetMap
data, no key required — same source restaurant_scraper.py and
attraction_scraper.py use), falls back to seeded mock data on any failure.

OSM carries real hotel names, addresses, and sometimes stars/room counts —
but never nightly rates. Like attraction entry fees, pricing is deliberately
left None for live results rather than guessed; a dedicated pricing
specialist is expected to fill it in per-recommendation later.

Sort options:
    price      — cheapest nightly rate first (unpriced results sort last)
    rating     — highest guest review score first (mock data only; live data
                 has no rating)
    proximity  — closest to city centre first (unpriced results sort last)
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

HOTEL_NAMES = [
    # Generic / European
    "The Grand", "City Suites", "Harbour View", "Central Plaza",
    "The Riverside Inn", "Park Hotel", "Old Town Lodge", "Metro Boutique",
    # Indian hotel brands & names
    "Taj", "Oberoi", "ITC Grand", "The Leela", "Marriott",
    "Hyatt Regency", "Lemon Tree", "Radisson Blu",
]

# City-specific hotel overrides — richer names seeded per location
_CITY_HOTELS: dict[str, list[str]] = {
    "chennai":    ["Taj Coromandel", "ITC Grand Chola", "The Leela Palace", "Hyatt Regency Chennai", "Radisson Blu Chennai City Centre"],
    "mumbai":     ["Taj Mahal Palace", "Oberoi Mumbai", "ITC Grand Central", "The Leela Mumbai", "Trident Nariman Point"],
    "delhi":      ["The Imperial", "Taj Mahal Hotel Delhi", "Oberoi New Delhi", "ITC Maurya", "The Leela Palace Delhi"],
    "hyderabad":  ["Taj Falaknuma Palace", "ITC Kohenur", "Marriott Hyderabad", "Hyatt Hyderabad Gachibowli", "Radisson Blu Plaza Hyderabad"],
    "bengaluru":  ["Taj West End", "ITC Windsor", "The Leela Palace Bengaluru", "Oberoi Bengaluru", "Marriott Bengaluru Whitefield"],
    "bangalore":  ["Taj West End", "ITC Windsor", "The Leela Palace Bengaluru", "Oberoi Bengaluru", "Marriott Bengaluru Whitefield"],
    "lucknow":    ["Taj Hotel & Convention Centre", "Lebua Lucknow", "Radisson Lucknow City Centre", "Hyatt Regency Lucknow", "Lemon Tree Premier Lucknow"],
    "patna":      ["Maurya Hotel Patna", "Hotel Chanakya Patna", "Radisson Blu Patna", "Hotel Patliputra Ashok", "Lemon Tree Patna"],
}

AMENITIES_POOL = [
    "Free WiFi", "Breakfast included", "Gym", "Pool", "Parking",
    "Bar", "Restaurant", "Spa", "Airport shuttle", "Pet-friendly",
    # Indian hotel amenities
    "Rooftop pool", "Ayurvedic spa", "24hr in-room dining",
    "Concierge", "Business centre", "Valet parking", "Butler service",
]

# Indian cities have lower nightly rates in GBP
_INDIAN_CITIES = {
    "chennai", "mumbai", "delhi", "hyderabad", "bengaluru",
    "bangalore", "lucknow", "patna", "kolkata", "ahmedabad", "pune", "goa",
}


def _seeded_hotels(location: str, check_in: str, check_out: str) -> list[dict[str, Any]]:
    """Generate deterministic mock hotel results seeded from location + dates."""
    seed = int(hashlib.md5(f"{location}{check_in}{check_out}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    nights = _nights(check_in, check_out)
    loc_lower = location.lower()
    is_indian = loc_lower in _INDIAN_CITIES
    city_hotels = _CITY_HOTELS.get(loc_lower, [])

    results = []
    for i in range(6):
        if city_hotels and i < len(city_hotels):
            name = city_hotels[i]
        else:
            name = rng.choice(HOTEL_NAMES) + (f" {rng.choice(['&', 'and'])} Spa" if i % 3 == 0 else "")
        stars = rng.choice([3, 3, 4, 4, 4, 5])
        rating = round(rng.uniform(7.0, 9.8), 1)
        # Indian hotels are cheaper in GBP due to exchange rate
        price_range = (15, 120) if is_indian else (60, 400)
        price_per_night = round(rng.uniform(*price_range) * (stars / 3), 2)
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


_OSM_TOURISM_VALUES = ("hotel", "guest_house", "hostel", "motel")


async def _fetch_hotels(location: str, check_in: str, check_out: str) -> list[dict[str, Any]]:
    """Attempt Overpass API query; fall back to mock data on any failure.

    Node-only (no way/relation geometries) — testing showed way-tagged
    "hotel" buildings pull in a lot of mistagged/irrelevant OSM data in some
    regions, while nodes give clean, real results.
    """
    try:
        import httpx

        tourism_filter = "|".join(_OSM_TOURISM_VALUES)
        query = (
            f'[out:json][timeout:10];'
            f'area[name="{location}"]->.a;'
            f'node["tourism"~"{tourism_filter}"](area.a);'
            f'out body 10;'
        )
        # Overpass/OSM infra rejects requests with no (or a generic) User-Agent.
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Smartinerary/0.1"}) as client:
            response = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
            )
            if response.status_code == 200:
                elements = response.json().get("elements", [])
                if elements:
                    return _normalise_overpass(elements, location, check_in, check_out)
    except Exception as exc:
        logger.debug("Hotel HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_hotels(location, check_in, check_out)


def _parse_int(value: Any) -> int | None:
    """Best-effort int parse for OSM tag values like "4" or self-assessed "4s"."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def _normalise_overpass(
    elements: list[dict], location: str, check_in: str, check_out: str,
) -> list[dict[str, Any]]:
    """Map Overpass/OSM fields to our internal hotel schema.

    OSM has no nightly-rate data at all (not even a qualitative band like
    restaurants' price_range), so price_per_night_gbp/total_price_gbp come
    back None for live results — left for a pricing specialist to fill in.
    """
    nights = _nights(check_in, check_out)
    results = []
    for i, el in enumerate(elements):
        tags = el.get("tags", {})
        amenities = []
        if tags.get("internet_access") in ("wlan", "yes"):
            amenities.append("Free WiFi")
        if tags.get("wheelchair") == "yes":
            amenities.append("Wheelchair accessible")
        if "parking" in tags:
            amenities.append("Parking")
        if tags.get("smoking") == "no":
            amenities.append("Non-smoking")

        results.append({
            "id": f"OSM-{el.get('id', i)}",
            "name": tags.get("name", "Unnamed Hotel"),
            "location": location,
            "stars": _parse_int(tags.get("stars")),
            "rating": None,
            "review_count": None,
            "price_per_night_gbp": None,
            "total_price_gbp": None,
            "nights": nights,
            "distance_from_centre_km": None,
            "amenities": amenities,
            "check_in": check_in,
            "check_out": check_out,
            "rooms_available": _parse_int(tags.get("rooms")),
        })
    return results


def _sort(hotels: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    if sort_by == "price":
        return sorted(
            hotels,
            key=lambda h: h["price_per_night_gbp"] if h.get("price_per_night_gbp") is not None else float("inf"),
        )
    if sort_by == "rating":
        return sorted(hotels, key=lambda h: h.get("rating") or 0, reverse=True)
    if sort_by == "proximity":
        return sorted(
            hotels,
            key=lambda h: h["distance_from_centre_km"] if h.get("distance_from_centre_km") is not None else float("inf"),
        )
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
