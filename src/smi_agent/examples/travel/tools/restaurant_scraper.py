"""Restaurant search tool — scrapes/queries for restaurants at a location.

Prototype implementation: attempts a real HTTP request to the Overpass API
(OpenStreetMap data, no key required), falls back to seeded mock data.

Sort options:
    rating     — highest review score first
    price      — cheapest average spend per person first
    match      — closest cuisine match to stated preference first
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

CUISINES = ["Italian", "French", "Japanese", "Indian", "Thai", "Mexican",
            "British", "Mediterranean", "Chinese", "Greek", "Spanish", "American"]
PRICE_BANDS = ["£", "££", "£££", "££££"]
HIGHLIGHTS = [
    "Known for seasonal tasting menus",
    "Popular with locals, book ahead",
    "Great outdoor terrace",
    "Michelin-recommended",
    "Excellent wine list",
    "Good vegetarian options",
    "Quick service, ideal for lunch",
    "Live music on weekends",
]


def _seeded_restaurants(location: str, cuisine: str | None) -> list[dict[str, Any]]:
    """Generate deterministic mock restaurant results."""
    seed = int(hashlib.md5(f"{location}{cuisine or ''}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    results = []
    for i in range(8):
        c = cuisine if cuisine and rng.random() > 0.3 else rng.choice(CUISINES)
        price_band = rng.choices(PRICE_BANDS, weights=[30, 40, 20, 10])[0]
        avg_spend = {"£": rng.randint(10, 20), "££": rng.randint(20, 40),
                     "£££": rng.randint(40, 80), "££££": rng.randint(80, 150)}[price_band]
        results.append({
            "id": f"RST-{seed % 10000:04d}-{i}",
            "name": f"{rng.choice(['The', 'La', 'Le', 'Il', ''])} {c} {'House' if i % 2 == 0 else 'Kitchen'}".strip(),
            "cuisine": c,
            "location": location,
            "rating": round(rng.uniform(6.0, 9.9), 1),
            "review_count": rng.randint(20, 2000),
            "price_band": price_band,
            "avg_spend_per_person_gbp": avg_spend,
            "distance_from_location_km": round(rng.uniform(0.1, 3.0), 1),
            "highlight": rng.choice(HIGHLIGHTS),
            "booking_required": rng.random() > 0.5,
            "cuisine_match": cuisine is not None and c.lower() == cuisine.lower(),
        })
    return results


async def search_restaurants(
    location: str,
    cuisine: str | None = None,
    sort_by: str = "rating",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for restaurants near a location.

    Prototype: tries the Overpass API (OSM data) first; falls back to mock data.

    Args:
        location:    City or area to search in (e.g. "Paris 8th", "Edinburgh Old Town").
        cuisine:     Optional cuisine filter (e.g. "Italian", "Japanese").
        sort_by:     Ranking preference — "rating", "price", or "match".
        num_results: Maximum number of results to return (default 5).

    Returns:
        List of restaurant dicts, each with: id, name, cuisine, rating,
        price_band, avg_spend_per_person_gbp, distance_from_location_km, highlight.
    """
    restaurants = await _fetch_restaurants(location, cuisine)
    restaurants = _sort(restaurants, sort_by, cuisine)
    return restaurants[:num_results]


async def _fetch_restaurants(location: str, cuisine: str | None) -> list[dict[str, Any]]:
    """Attempt Overpass API query; fall back to mock data on any failure."""
    try:
        import httpx

        # Overpass API: query OSM for restaurants in the named area.
        cuisine_filter = f'["cuisine"="{cuisine.lower()}"]' if cuisine else ""
        query = (
            f'[out:json][timeout:10];'
            f'area[name="{location}"]->.a;'
            f'node["amenity"="restaurant"]{cuisine_filter}(area.a);'
            f'out body 10;'
        )
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
            )
            if response.status_code == 200:
                elements = response.json().get("elements", [])
                if elements:
                    return _normalise_overpass(elements, location)
    except Exception as exc:
        logger.debug("Restaurant HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_restaurants(location, cuisine)


def _normalise_overpass(elements: list[dict], location: str) -> list[dict[str, Any]]:
    """Map Overpass/OSM fields to our internal restaurant schema."""
    results = []
    for i, el in enumerate(elements):
        tags = el.get("tags", {})
        results.append({
            "id": f"OSM-{el.get('id', i)}",
            "name": tags.get("name", "Unnamed Restaurant"),
            "cuisine": tags.get("cuisine", "Unknown").replace(";", ", ").title(),
            "location": location,
            "rating": None,         # OSM does not carry ratings
            "review_count": None,
            "price_band": tags.get("price_range", "££"),
            "avg_spend_per_person_gbp": None,
            "distance_from_location_km": None,
            "highlight": tags.get("description", ""),
            "booking_required": False,
            "cuisine_match": False,
        })
    return results


def _sort(restaurants: list[dict[str, Any]], sort_by: str, cuisine: str | None) -> list[dict[str, Any]]:
    if sort_by == "rating":
        return sorted(restaurants, key=lambda r: r.get("rating") or 0, reverse=True)
    if sort_by == "price":
        return sorted(restaurants, key=lambda r: r.get("avg_spend_per_person_gbp") or 999)
    if sort_by == "match":
        # Exact cuisine matches first, then by rating
        return sorted(
            restaurants,
            key=lambda r: (not r.get("cuisine_match", False), -(r.get("rating") or 0)),
        )
    return restaurants


SEARCH_RESTAURANTS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "search_restaurants",
        "description": (
            "Search for restaurants near a location, optionally filtered by cuisine. "
            "Returns a ranked list with ratings, price bands, and highlights."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City, area, or landmark to search near (e.g. 'Paris 8th', 'Edinburgh Old Town')",
                },
                "cuisine": {
                    "type": "string",
                    "description": "Optional cuisine type filter (e.g. 'Italian', 'Japanese', 'vegetarian')",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["rating", "price", "match"],
                    "description": "Ranking preference. Default is 'rating'.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5)",
                },
            },
            "required": ["location"],
        },
    },
}
