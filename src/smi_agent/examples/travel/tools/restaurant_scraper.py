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

CUISINES = [
    "Italian", "French", "Japanese", "Indian", "Thai", "Mexican",
    "British", "Mediterranean", "Chinese", "Greek", "Spanish", "American",
    # Indian regional cuisines
    "South Indian", "North Indian", "Chettinad", "Hyderabadi", "Mughlai",
    "Awadhi", "Punjabi", "Coastal", "Bihari", "Andhra",
]
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
    # Indian-specific
    "Famous for biriyani",
    "Best dosa in the city",
    "Iconic street food spot",
    "Legendary kebabs since 1905",
    "Rooftop with city views",
    "Pure vegetarian thali",
    "Award-winning chef",
]

# City-specific restaurant lists — realistic names per city
_CITY_RESTAURANTS: dict[str, list[dict]] = {
    "chennai": [
        {"name": "Murugan Idli Shop",    "cuisine": "South Indian",  "price_band": "£",   "highlight": "Iconic for fluffy idlis and sambar, a Chennai institution"},
        {"name": "Dakshin",              "cuisine": "South Indian",  "price_band": "£££", "highlight": "Refined regional South Indian at ITC Grand Chola"},
        {"name": "Saravana Bhavan",      "cuisine": "South Indian",  "price_band": "£",   "highlight": "Legendary vegetarian chain — try the meals thali"},
        {"name": "Peshwari",             "cuisine": "North Indian",  "price_band": "£££", "highlight": "Award-winning dal bukhara and kebabs"},
        {"name": "The Marina",           "cuisine": "Coastal",       "price_band": "££",  "highlight": "Fresh seafood overlooking the beach promenade"},
        {"name": "Chettinad Restaurant", "cuisine": "Chettinad",     "price_band": "££",  "highlight": "Fiery Clos pepper chicken and kuzhi paniyaram"},
        {"name": "Burma Burma",          "cuisine": "Burmese",       "price_band": "££",  "highlight": "Vegetarian-friendly, great tea leaf salad"},
        {"name": "Junior Kuppanna",      "cuisine": "Chettinad",     "price_band": "£",   "highlight": "Best mutton biryani in the city, book ahead"},
    ],
    "mumbai": [
        {"name": "Trishna",              "cuisine": "Coastal",       "price_band": "£££", "highlight": "Celebrated butter garlic crab, a Mumbai landmark"},
        {"name": "Britannia & Co",       "cuisine": "Parsi",         "price_band": "££",  "highlight": "Berry pulao and salli boti — Parsi heritage since 1923"},
        {"name": "Leopold Cafe",         "cuisine": "Continental",   "price_band": "££",  "highlight": "Colaba institution, open since 1871"},
        {"name": "Khyber",               "cuisine": "Mughlai",       "price_band": "£££", "highlight": "Grand murals and exceptional raan — book ahead"},
        {"name": "Swati Snacks",         "cuisine": "Gujarati",      "price_band": "£",   "highlight": "Best pani puri and handvo in the city"},
        {"name": "Peshwa",               "cuisine": "Maharashtrian", "price_band": "££",  "highlight": "Traditional thali with sol kadi and puran poli"},
        {"name": "The Table",            "cuisine": "Contemporary",  "price_band": "££££","highlight": "Farm-to-table modern Indian, one of Asia's 50 best"},
        {"name": "Mohammad Ali Road Stalls","cuisine": "Mughlai",    "price_band": "£",   "highlight": "Legendary street food — nalli nihari and seekh kebabs"},
    ],
    "delhi": [
        {"name": "Bukhara",              "cuisine": "North Indian",  "price_band": "££££","highlight": "Dal bukhara slow-cooked 18 hours — world-famous"},
        {"name": "Karim's",              "cuisine": "Mughlai",       "price_band": "£",   "highlight": "Old Delhi legend since 1913 — mutton korma and seekh kebab"},
        {"name": "Indian Accent",        "cuisine": "Contemporary",  "price_band": "££££","highlight": "India's top-ranked restaurant — inventive modern Indian"},
        {"name": "Saravana Bhavan",      "cuisine": "South Indian",  "price_band": "£",   "highlight": "Reliable vegetarian South Indian — great set meals"},
        {"name": "Paranthe Wali Gali",   "cuisine": "North Indian",  "price_band": "£",   "highlight": "Iconic Old Delhi lane famous for stuffed parathas since 1872"},
        {"name": "Dum Pukht",            "cuisine": "Awadhi",        "price_band": "££££","highlight": "Slow-cooked biryani in sealed vessels — regal Awadhi cuisine"},
        {"name": "Moti Mahal",           "cuisine": "Mughlai",       "price_band": "££",  "highlight": "Birthplace of butter chicken — the original since 1947"},
        {"name": "Gulati",               "cuisine": "Punjabi",       "price_band": "££",  "highlight": "Daryaganj favourite for dal makhani and tandoori chicken"},
    ],
    "hyderabad": [
        {"name": "Paradise Restaurant",  "cuisine": "Hyderabadi",    "price_band": "£",   "highlight": "The city's most iconic biryani — queues are part of the experience"},
        {"name": "Bawarchi",             "cuisine": "Hyderabadi",    "price_band": "£",   "highlight": "Legendary mutton biryani cooked in traditional dum style"},
        {"name": "Shah Ghouse",          "cuisine": "Hyderabadi",    "price_band": "£",   "highlight": "Famous haleem and Irani chai near the old city"},
        {"name": "Fusion 9",             "cuisine": "Contemporary",  "price_band": "£££", "highlight": "Modern pan-Asian with city skyline views"},
        {"name": "Jewel of Nizam",       "cuisine": "Hyderabadi",    "price_band": "£££", "highlight": "Royal Nizami cuisine in a heritage setting"},
        {"name": "Chutneys",             "cuisine": "South Indian",  "price_band": "£",   "highlight": "Outstanding dosas and idli — favourite for breakfast"},
        {"name": "Rayalaseema Ruchulu",  "cuisine": "Andhra",        "price_band": "£",   "highlight": "Fiery Andhra meals — gongura mutton and ragi sangati"},
        {"name": "Ohri's Jiva Imperia",  "cuisine": "Mughlai",       "price_band": "££",  "highlight": "Rooftop dining with views of Hussain Sagar lake"},
    ],
    "bengaluru": [
        {"name": "MTR (Mavalli Tiffin Rooms)","cuisine": "South Indian","price_band": "£", "highlight": "Legendary 1924 breakfast spot — rava idli was invented here"},
        {"name": "Koshy's",              "cuisine": "Continental",   "price_band": "££",  "highlight": "Bengaluru institution since 1940 — bacon and eggs, draught beer"},
        {"name": "The Only Place",       "cuisine": "Continental",   "price_band": "££",  "highlight": "Best steaks in the city — beloved expat favourite since 1959"},
        {"name": "Vidyarthi Bhavan",     "cuisine": "South Indian",  "price_band": "£",   "highlight": "Crispy masala dosa — expect a queue, worth every minute"},
        {"name": "Karavalli",            "cuisine": "Coastal",       "price_band": "£££", "highlight": "Award-winning Coastal Karnataka and Kerala cuisine"},
        {"name": "Truffles",             "cuisine": "American",      "price_band": "££",  "highlight": "Best burgers in Bengaluru — always packed, no reservations"},
        {"name": "Brahmin's Coffee Bar", "cuisine": "South Indian",  "price_band": "£",   "highlight": "Tiny Basavanagudi spot — idli vada and filter coffee since 1965"},
        {"name": "Ebony",                "cuisine": "North Indian",  "price_band": "£££", "highlight": "Rooftop terrace with panoramic city views and live ghazals"},
    ],
    "lucknow": [
        {"name": "Tunday Kababi",        "cuisine": "Awadhi",        "price_band": "£",   "highlight": "120-year-old kabab legend — galouti and shami since 1905"},
        {"name": "Dastarkhwan",          "cuisine": "Awadhi",        "price_band": "£",   "highlight": "Authentic Lucknowi biryani and nihari, a city favourite"},
        {"name": "Oudhyana",             "cuisine": "Awadhi",        "price_band": "£££", "highlight": "Refined Nawabi cuisine at the Taj — dum biryani in sealed handis"},
        {"name": "Royal Cafe",           "cuisine": "Awadhi",        "price_band": "£",   "highlight": "Basket chaat and tokri chaat — Hazratganj street food classic"},
        {"name": "Wahid Biryani",        "cuisine": "Awadhi",        "price_band": "£",   "highlight": "Pacchikaan biryani — meat cooked raw with rice in true Awadhi style"},
        {"name": "Idris ki Biryani",     "cuisine": "Awadhi",        "price_band": "£",   "highlight": "Old City hole-in-the-wall — best beef biryani in Lucknow"},
        {"name": "Moti Mahal",           "cuisine": "Mughlai",       "price_band": "££",  "highlight": "Butter chicken and roomali roti, long Lucknow heritage"},
        {"name": "Chowk Street Food",    "cuisine": "Awadhi",        "price_band": "£",   "highlight": "The beating heart of Lucknow's culinary tradition"},
    ],
    "patna": [
        {"name": "Hotel Maurya Restaurant","cuisine": "North Indian", "price_band": "££", "highlight": "Best dal baati churma and litti chokha in Patna"},
        {"name": "Sone Ki Chidiya",      "cuisine": "Bihari",        "price_band": "£",   "highlight": "Authentic Bihari thali — sattu paratha and chura dahi"},
        {"name": "Kesar Restaurant",     "cuisine": "Mughlai",       "price_band": "££",  "highlight": "Reliable kebabs and biryani — popular with local families"},
        {"name": "Bihari Litti Corner",  "cuisine": "Bihari",        "price_band": "£",   "highlight": "Famous for litti chokha — a Bihari staple cooked over coal"},
        {"name": "Rajhans Restaurant",   "cuisine": "North Indian",  "price_band": "££",  "highlight": "Wholesome North Indian meals and fresh lassi"},
        {"name": "Street Food at Gandhi Maidan","cuisine": "Bihari", "price_band": "£",   "highlight": "Evening street market — chaat, makhana, and sattu drinks"},
        {"name": "New Punjab Hotel",     "cuisine": "Punjabi",       "price_band": "£",   "highlight": "Butter chicken and naan — solid Punjabi food since 1980"},
        {"name": "Bangali Sweets",       "cuisine": "Bihari",        "price_band": "£",   "highlight": "Legendary mithai — khaja and laddoos for over 50 years"},
    ],
}


# Spend-per-person GBP ranges backing each price_band symbol. Kept at module
# level so both the mock generator and the live-data normaliser (which only
# gets a qualitative price_range tag back from Overpass, never a number) can
# convert a symbol into an actual figure the Budget Agent can do maths on.
_SPEND_RANGES_GBP = {"£": (10, 20), "££": (20, 40), "£££": (40, 80), "££££": (80, 150)}
_SPEND_RANGES_GBP_INDIA = {"£": (3, 8), "££": (8, 20), "£££": (20, 50), "££££": (50, 120)}


def _price_band_to_avg_spend_gbp(price_band: str, indian: bool = False) -> float:
    """Midpoint GBP estimate for a price-band symbol.

    Used when a data source supplies only the qualitative symbol (e.g. OSM's
    price_range tag) and no actual spend figure, so cost calculations always
    have a real number to work with instead of a "£"-style stand-in.
    """
    ranges = _SPEND_RANGES_GBP_INDIA if indian else _SPEND_RANGES_GBP
    lo, hi = ranges.get(price_band, ranges["££"])
    return round((lo + hi) / 2, 2)


def _seeded_restaurants(location: str, cuisine: str | None) -> list[dict[str, Any]]:
    """Generate deterministic mock restaurant results, using city-specific data where available."""
    seed = int(hashlib.md5(f"{location}{cuisine or ''}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    city_list = _CITY_RESTAURANTS.get(location.lower(), [])

    # Indian cities have lower average spend in GBP
    indian_city = location.lower() in _CITY_RESTAURANTS
    spend_ranges = _SPEND_RANGES_GBP_INDIA if indian_city else _SPEND_RANGES_GBP

    results = []
    for i in range(8):
        if city_list and i < len(city_list):
            # Use real city restaurant data
            r = city_list[i]
            c = r["cuisine"]
            price_band = r["price_band"]
            highlight = r["highlight"]
            name = r["name"]
        else:
            c = cuisine if cuisine and rng.random() > 0.3 else rng.choice(CUISINES)
            price_band = rng.choices(PRICE_BANDS, weights=[30, 40, 20, 10])[0]
            highlight = rng.choice(HIGHLIGHTS)
            name = f"{rng.choice(['The', 'La', 'Le', 'Il', ''])} {c} {'House' if i % 2 == 0 else 'Kitchen'}".strip()

        lo, hi = spend_ranges[price_band]
        avg_spend = rng.randint(lo, hi)
        # Business-friendly proxy: pricier venues with sit-down service tend to
        # suit client dining/work meals better than quick street-food spots.
        business_friendly = price_band in ("£££", "££££") or rng.random() > 0.6

        results.append({
            "id": f"RST-{seed % 10000:04d}-{i}",
            "name": name,
            "cuisine": c,
            "location": location,
            "rating": round(rng.uniform(7.5, 9.9), 1),
            "review_count": rng.randint(50, 5000),
            "price_band": price_band,
            "avg_spend_per_person_gbp": avg_spend,
            "distance_from_location_km": round(rng.uniform(0.1, 3.0), 1),
            "highlight": highlight,
            "booking_required": rng.random() > 0.5,
            "cuisine_match": cuisine is not None and c.lower() == cuisine.lower(),
            "business_friendly": business_friendly,
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
        logger.debug("Restaurant HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_restaurants(location, cuisine)


def _normalise_overpass(elements: list[dict], location: str) -> list[dict[str, Any]]:
    """Map Overpass/OSM fields to our internal restaurant schema.

    OSM only ever gives us a qualitative price_range tag ("£".."££££"), never
    an actual spend figure, so avg_spend_per_person_gbp is derived from that
    symbol rather than left as None — every restaurant needs a real number
    for the Budget Agent's cost maths, not just a "how expensive" indicator.
    """
    indian = location.lower() in _CITY_RESTAURANTS
    results = []
    for i, el in enumerate(elements):
        tags = el.get("tags", {})
        price_band = tags.get("price_range", "££")
        if price_band not in _SPEND_RANGES_GBP:
            price_band = "££"
        results.append({
            "id": f"OSM-{el.get('id', i)}",
            "name": tags.get("name", "Unnamed Restaurant"),
            "cuisine": tags.get("cuisine", "Unknown").replace(";", ", ").title(),
            "location": location,
            "rating": None,         # OSM does not carry ratings
            "review_count": None,
            "price_band": price_band,
            "avg_spend_per_person_gbp": _price_band_to_avg_spend_gbp(price_band, indian=indian),
            "distance_from_location_km": None,
            "highlight": tags.get("description", ""),
            "booking_required": False,
            "cuisine_match": False,
            "business_friendly": False,
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
