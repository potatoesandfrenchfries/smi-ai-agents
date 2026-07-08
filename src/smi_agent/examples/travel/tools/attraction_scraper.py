"""Attraction search tool — scrapes/queries for tourist attractions and experiences.

Prototype implementation: attempts a real Overpass API query (OpenStreetMap
data, no key required — same source restaurant_scraper.py uses), falls back
to seeded mock data on any failure.

Entry fees are deliberately NOT sourced here — OSM's tourism tags don't carry
structured pricing, so entry_fee_gbp comes back None for live results. That's
left for a dedicated pricing specialist to fill in per-recommendation later;
this tool's job is discovery (name, category, location), not pricing.

Sort options:
    rating     — highest-rated first (mock data only; live data has no rating)
    price      — cheapest entry fee first (unpriced results sort last)
    proximity  — closest to city centre first (unpriced results sort last)
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

CATEGORIES = [
    "Landmark", "Museum", "Park", "Gallery", "Historic Site",
    "Market", "Viewpoint", "Cultural Experience", "Religious Site", "Adventure",
]

TAGS_POOL = [
    "sightseeing", "culture", "relaxation", "family-friendly",
    "outdoor", "indoor", "free-entry", "photo-spot", "adventure", "history",
]

HIGHLIGHTS = [
    "Iconic photo stop, best visited early morning",
    "Local favourite, less crowded on weekdays",
    "Guided tours available on the hour",
    "Free entry, donations welcome",
    "Best experienced at sunset",
    "Popular with families, allow half a day",
    "UNESCO World Heritage Site",
    "Skip-the-line tickets recommended in peak season",
]

# City-specific attraction lists — real landmarks per city, matching the
# quality bar set by _CITY_HOTELS / _CITY_RESTAURANTS in the sibling scrapers.
_CITY_ATTRACTIONS: dict[str, list[dict]] = {
    "chennai": [
        {"name": "Marina Beach",              "category": "Landmark",   "tags": ["sightseeing", "outdoor", "free-entry"], "highlight": "One of the world's longest urban beaches — best at sunrise"},
        {"name": "Kapaleeshwarar Temple",      "category": "Religious Site", "tags": ["culture", "history", "photo-spot"], "highlight": "Vivid Dravidian gopuram, active temple since the 7th century"},
        {"name": "Fort St. George",            "category": "Historic Site", "tags": ["history", "indoor"], "highlight": "First English fortress in India, now a museum"},
        {"name": "Government Museum Egmore",   "category": "Museum",    "tags": ["culture", "indoor"], "highlight": "Bronze gallery is one of the finest in the country"},
        {"name": "Elliot's Beach",             "category": "Park",      "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Quieter than Marina, great for an evening walk"},
        {"name": "DakshinaChitra Heritage Museum", "category": "Cultural Experience", "tags": ["culture", "family-friendly"], "highlight": "Living museum of South Indian heritage crafts"},
    ],
    "mumbai": [
        {"name": "Gateway of India",           "category": "Landmark",   "tags": ["sightseeing", "photo-spot", "history"], "highlight": "Mumbai's most photographed monument, facing the harbour"},
        {"name": "Chhatrapati Shivaji Museum",  "category": "Museum",    "tags": ["culture", "indoor"], "highlight": "Indo-Saracenic architecture with an extensive art collection"},
        {"name": "Marine Drive",               "category": "Viewpoint", "tags": ["sightseeing", "relaxation", "free-entry"], "highlight": "The 'Queen's Necklace' — best walked at sunset"},
        {"name": "Elephanta Caves",             "category": "Historic Site", "tags": ["history", "adventure"], "highlight": "UNESCO rock-cut caves, reached by a short ferry ride"},
        {"name": "Sanjay Gandhi National Park",  "category": "Park",     "tags": ["outdoor", "family-friendly", "adventure"], "highlight": "Urban forest with caves, trails, and a lion safari"},
        {"name": "Crawford Market",             "category": "Market",   "tags": ["culture", "sightseeing"], "highlight": "Colonial-era market, great for spices and souvenirs"},
    ],
    "delhi": [
        {"name": "Red Fort",                   "category": "Historic Site", "tags": ["history", "photo-spot"], "highlight": "UNESCO Mughal fortress, site of the Independence Day flag hoisting"},
        {"name": "Qutub Minar",                "category": "Landmark",  "tags": ["history", "photo-spot"], "highlight": "Tallest brick minaret in the world, UNESCO listed"},
        {"name": "Humayun's Tomb",              "category": "Historic Site", "tags": ["history", "culture"], "highlight": "Precursor to the Taj Mahal's design"},
        {"name": "India Gate",                 "category": "Landmark",  "tags": ["sightseeing", "free-entry", "family-friendly"], "highlight": "War memorial, lively in the evenings with food stalls nearby"},
        {"name": "Lodhi Gardens",               "category": "Park",     "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Peaceful gardens with 15th-century tombs"},
        {"name": "Chandni Chowk",               "category": "Market",   "tags": ["culture", "sightseeing"], "highlight": "Old Delhi's bustling historic market lane"},
    ],
    "hyderabad": [
        {"name": "Charminar",                  "category": "Landmark",  "tags": ["history", "photo-spot"], "highlight": "16th-century icon at the heart of the old city"},
        {"name": "Golconda Fort",               "category": "Historic Site", "tags": ["history", "adventure"], "highlight": "Legendary acoustics — clap at the entrance, hear it at the top"},
        {"name": "Hussain Sagar Lake",           "category": "Viewpoint", "tags": ["sightseeing", "relaxation", "free-entry"], "highlight": "Boat rides to the Buddha statue at the lake's centre"},
        {"name": "Salar Jung Museum",            "category": "Museum",   "tags": ["culture", "indoor"], "highlight": "One of the largest one-man art collections in the world"},
        {"name": "Ramoji Film City",             "category": "Cultural Experience", "tags": ["family-friendly", "outdoor"], "highlight": "World's largest film studio complex, full-day theme park"},
        {"name": "Chowmahalla Palace",           "category": "Historic Site", "tags": ["history", "culture"], "highlight": "Former seat of the Nizams, restored courtyards and chandeliers"},
    ],
    "bengaluru": [
        {"name": "Lalbagh Botanical Garden",     "category": "Park",     "tags": ["relaxation", "outdoor", "family-friendly"], "highlight": "240-acre garden with a glasshouse modelled on Crystal Palace"},
        {"name": "Bangalore Palace",             "category": "Historic Site", "tags": ["history", "photo-spot"], "highlight": "Tudor-style palace with audio-guided tours"},
        {"name": "Cubbon Park",                  "category": "Park",     "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Green escape in the middle of the city, great for a morning walk"},
        {"name": "Vidhana Soudha",               "category": "Landmark", "tags": ["sightseeing", "photo-spot"], "highlight": "Illuminated state legislature building, striking at night"},
        {"name": "ISKCON Temple Bangalore",       "category": "Religious Site", "tags": ["culture", "family-friendly"], "highlight": "Grand modern temple complex on Hare Krishna Hill"},
        {"name": "Wonderla Bengaluru",            "category": "Adventure", "tags": ["adventure", "family-friendly"], "highlight": "Amusement park with water rides, full-day outing"},
    ],
    "london": [
        {"name": "British Museum",              "category": "Museum",   "tags": ["culture", "indoor", "free-entry"], "highlight": "Free entry, world-renowned collection including the Rosetta Stone"},
        {"name": "Tower of London",              "category": "Historic Site", "tags": ["history", "photo-spot"], "highlight": "UNESCO site, home to the Crown Jewels"},
        {"name": "London Eye",                  "category": "Viewpoint", "tags": ["sightseeing", "photo-spot"], "highlight": "Panoramic views over the Thames and Westminster"},
        {"name": "Hyde Park",                   "category": "Park",     "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Central green space, great for a picnic or Serpentine walk"},
        {"name": "Tate Modern",                 "category": "Gallery",  "tags": ["culture", "indoor", "free-entry"], "highlight": "Contemporary art in a converted power station"},
        {"name": "Camden Market",               "category": "Market",   "tags": ["culture", "sightseeing"], "highlight": "Eclectic stalls, street food, and live music"},
    ],
    "paris": [
        {"name": "Eiffel Tower",                "category": "Landmark", "tags": ["sightseeing", "photo-spot"], "highlight": "Book timed-entry tickets in advance to skip queues"},
        {"name": "Louvre Museum",               "category": "Museum",   "tags": ["culture", "indoor"], "highlight": "Home to the Mona Lisa — arrive at opening to beat crowds"},
        {"name": "Montmartre & Sacré-Cœur",      "category": "Viewpoint", "tags": ["sightseeing", "photo-spot", "history"], "highlight": "Hilltop basilica with the best free view over Paris"},
        {"name": "Musée d'Orsay",               "category": "Gallery",  "tags": ["culture", "indoor"], "highlight": "Impressionist masterpieces in a former railway station"},
        {"name": "Jardin du Luxembourg",         "category": "Park",     "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Elegant gardens, popular with locals for a quiet break"},
        {"name": "Seine River Cruise",           "category": "Cultural Experience", "tags": ["sightseeing", "relaxation"], "highlight": "An hour-long cruise past most major landmarks"},
    ],
    "edinburgh": [
        {"name": "Edinburgh Castle",             "category": "Historic Site", "tags": ["history", "photo-spot"], "highlight": "Dominates the skyline, book ahead in summer"},
        {"name": "Arthur's Seat",               "category": "Viewpoint", "tags": ["outdoor", "adventure", "free-entry"], "highlight": "Extinct volcano with the best panoramic view of the city"},
        {"name": "Royal Mile",                  "category": "Landmark", "tags": ["sightseeing", "culture"], "highlight": "Historic street linking the Castle to Holyroodhouse"},
        {"name": "National Museum of Scotland",  "category": "Museum",   "tags": ["culture", "indoor", "free-entry"], "highlight": "Free entry, spans natural history to modern design"},
        {"name": "Princes Street Gardens",        "category": "Park",     "tags": ["relaxation", "outdoor", "free-entry"], "highlight": "Sits below the castle, lovely for a picnic"},
        {"name": "The Real Mary King's Close",    "category": "Cultural Experience", "tags": ["history", "adventure"], "highlight": "Guided tour through the city's hidden underground streets"},
    ],
}


def _seeded_attractions(location: str) -> list[dict[str, Any]]:
    """Generate deterministic mock attraction results, using city-specific data where available."""
    seed = int(hashlib.md5(f"attractions-{location}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    city_list = _CITY_ATTRACTIONS.get(location.lower(), [])

    results = []
    for i in range(6):
        if city_list and i < len(city_list):
            a = city_list[i]
            name = a["name"]
            category = a["category"]
            tags = a["tags"]
            highlight = a["highlight"]
        else:
            category = rng.choice(CATEGORIES)
            name = f"{location.title()} {category}"
            tags = rng.sample(TAGS_POOL, k=rng.randint(2, 4))
            highlight = rng.choice(HIGHLIGHTS)

        is_free = "free-entry" in tags or rng.random() < 0.2
        entry_fee = 0.0 if is_free else round(rng.uniform(5, 45), 2)

        results.append({
            "id": f"ATT-{seed % 10000:04d}-{i}",
            "name": name,
            "category": category,
            "location": location,
            "rating": round(rng.uniform(7.5, 9.9), 1),
            "review_count": rng.randint(50, 5000),
            "entry_fee_gbp": entry_fee,
            "duration_hours": round(rng.uniform(1.0, 4.0), 1),
            "distance_from_centre_km": round(rng.uniform(0.1, 10.0), 1),
            "tags": tags,
            "recommended_time_of_day": rng.choice(["morning", "afternoon", "evening"]),
            "highlight": highlight,
        })
    return results


_OSM_TOURISM_VALUES = (
    "attraction", "museum", "viewpoint", "gallery", "artwork", "zoo", "theme_park"
)


async def search_attractions(
    location: str,
    sort_by: str = "rating",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for tourist attractions and experiences near a location.

    Prototype: tries the Overpass API (OSM data) first; falls back to mock
    data on any failure (see module docstring re: entry fees).

    Args:
        location:    City, area, or landmark (e.g. "Paris", "Edinburgh").
        sort_by:     Ranking preference — "rating", "price", or "proximity".
        num_results: Maximum number of results to return (default 5).

    Returns:
        List of attraction dicts, each with: id, name, category, rating,
        entry_fee_gbp, duration_hours, distance_from_centre_km, tags,
        recommended_time_of_day, highlight.
    """
    attractions = await _fetch_attractions(location)
    attractions = _sort(attractions, sort_by)
    return attractions[:num_results]


async def _fetch_attractions(location: str) -> list[dict[str, Any]]:
    """Attempt Overpass API query; fall back to mock data on any failure."""
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
                    return _normalise_overpass(elements, location)
    except Exception as exc:
        logger.debug("Attraction HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_attractions(location)


def _normalise_overpass(elements: list[dict], location: str) -> list[dict[str, Any]]:
    """Map Overpass/OSM fields to our internal attraction schema.

    OSM carries no rating, review count, distance-from-centre, or pricing for
    these tags — those come back None. Entry fees are intentionally left for
    a separate pricing specialist to fill in per-recommendation (see module
    docstring); everything else here is real discovery data.
    """
    results = []
    for i, el in enumerate(elements):
        tags = el.get("tags", {})
        tourism_value = tags.get("tourism", "attraction")
        results.append({
            "id": f"OSM-{el.get('id', i)}",
            "name": tags.get("name", "Unnamed Attraction"),
            "category": tourism_value.replace("_", " ").title(),
            "location": location,
            "rating": None,
            "review_count": None,
            "entry_fee_gbp": None,
            "duration_hours": None,
            "distance_from_centre_km": None,
            "tags": [tourism_value],
            "recommended_time_of_day": None,
            "highlight": tags.get("description", ""),
        })
    return results


def _sort(attractions: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    if sort_by == "rating":
        return sorted(attractions, key=lambda a: a.get("rating") or 0, reverse=True)
    if sort_by == "price":
        return sorted(
            attractions,
            key=lambda a: a["entry_fee_gbp"] if a.get("entry_fee_gbp") is not None else float("inf"),
        )
    if sort_by == "proximity":
        return sorted(
            attractions,
            key=lambda a: a["distance_from_centre_km"] if a.get("distance_from_centre_km") is not None else float("inf"),
        )
    return attractions


SEARCH_ATTRACTIONS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "search_attractions",
        "description": (
            "Search for tourist attractions and experiences near a location. "
            "Returns a ranked list of options with entry fee, duration, and category."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City, area, or landmark (e.g. 'Paris', 'Edinburgh')",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["rating", "price", "proximity"],
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
