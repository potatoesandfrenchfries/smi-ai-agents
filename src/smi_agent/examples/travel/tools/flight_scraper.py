"""Flight search tool — scrapes/queries for available flights on a given route.

Prototype implementation: attempts a real HTTP request to a public flight data
source, falls back to seeded mock data so the agent always has something to
work with during development.

Sort options:
    cost      — cheapest fare first
    comfort   — fewest stops first, then full-service carriers
    time      — shortest total journey time first
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from smi_agent.cache.redis_cache import fingerprint, get_or_set
from smi_agent.config.redis_keys import flight_cache_key

logger = logging.getLogger(__name__)

# Fares/availability shift fast enough that a long TTL would go stale, but
# repeated searches for the same route+date within a short window (a user
# comparing options, an agent retrying) are common enough to be worth caching.
_CACHE_TTL_SECONDS = 300

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

AIRLINES = [
    # European / global
    "British Airways", "easyJet", "Ryanair", "Lufthansa", "KLM", "Air France",
    # Indian carriers
    "IndiGo", "Air India", "SpiceJet", "Vistara", "Akasa Air", "GoFirst",
]
AIRLINES_INDIAN = ["IndiGo", "Air India", "SpiceJet", "Vistara", "Akasa Air", "GoFirst"]
AIRCRAFT = [
    "Boeing 737", "Airbus A320", "Airbus A321", "Boeing 787", "Embraer E190",
    "Airbus A320neo", "Airbus A321neo",
]

# Base prices per route segment (GBP)
_BASE_PRICES = {"domestic_india": 30, "short": 60, "medium": 150, "long": 350}

# Indian domestic airports
_INDIAN = {"MAA", "BOM", "DEL", "HYD", "BLR", "LKO", "PAT", "CCU", "AMD", "PNQ", "COK", "GOI"}
# European airports
_EUROPEAN = {"EDI", "LHR", "LGW", "CDG", "AMS", "FRA", "MAD", "BCN", "FCO", "DUB"}


_CITY_TO_IATA = {
    "chennai": "MAA", "mumbai": "BOM", "delhi": "DEL",
    "hyderabad": "HYD", "bengaluru": "BLR", "bangalore": "BLR",
    "lucknow": "LKO", "patna": "PAT", "kolkata": "CCU",
    "ahmedabad": "AMD", "pune": "PNQ", "goa": "GOI", "kochi": "COK",
    "london": "LHR", "edinburgh": "EDI", "paris": "CDG",
    "amsterdam": "AMS", "berlin": "BER", "madrid": "MAD",
    "rome": "FCO", "new york": "JFK", "dubai": "DXB",
}


def _to_iata(city_or_code: str) -> str:
    """Resolve a city name or raw input to a 3-letter IATA code."""
    lower = city_or_code.lower().strip()
    if lower in _CITY_TO_IATA:
        return _CITY_TO_IATA[lower]
    return city_or_code.upper()[:3]


def _route_length(origin: str, destination: str) -> str:
    """Classify route as domestic_india / short / medium / long for pricing."""
    o, d = _to_iata(origin), _to_iata(destination)
    if o in _INDIAN and d in _INDIAN:
        return "domestic_india"
    if o in _EUROPEAN and d in _EUROPEAN:
        return "short"
    if any(x in (o, d) for x in ("JFK", "LAX", "SFO", "ORD", "YYZ")):
        return "long"
    if o in _INDIAN or d in _INDIAN:
        return "long"       # India ↔ international
    return "medium"


def _seeded_flights(origin: str, destination: str, date: str) -> list[dict[str, Any]]:
    """Generate deterministic mock flight results seeded from route + date."""
    seed = int(hashlib.md5(f"{origin}{destination}{date}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    length = _route_length(origin, destination)
    base_price = _BASE_PRICES[length]

    # Use Indian carriers for domestic India routes
    airline_pool = AIRLINES_INDIAN if length == "domestic_india" else AIRLINES

    results = []
    for i, hour in enumerate([6, 9, 12, 15, 19]):
        airline = rng.choice(airline_pool)
        stops = rng.choices([0, 1, 2], weights=[60, 30, 10])[0]
        duration_min = {
            "domestic_india": 90, "short": 90, "medium": 480, "long": 660,
        }[length] + stops * 90 + rng.randint(-20, 30)
        price = round(base_price * rng.uniform(0.8, 2.2) * (1 + stops * 0.15), 2)
        dep_h, dep_m = hour, rng.choice([0, 15, 30, 45])
        arr_total = dep_h * 60 + dep_m + duration_min
        arr_h, arr_m = (arr_total // 60) % 24, arr_total % 60

        results.append({
            "id": f"FLT-{seed % 10000:04d}-{i}",
            "airline": airline,
            "aircraft": rng.choice(AIRCRAFT),
            "origin": origin.upper()[:3],
            "destination": destination.upper()[:3],
            "date": date,
            "departure": f"{date}T{dep_h:02d}:{dep_m:02d}",
            "arrival": f"{date}T{arr_h:02d}:{arr_m:02d}",
            "duration_min": duration_min,
            "stops": stops,
            "price_gbp": price,
            "cabin": "economy",
            "seats_remaining": rng.randint(1, 50),
        })

    return results


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------

async def search_flights(
    origin: str,
    destination: str,
    date: str,
    sort_by: str = "cost",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Search for available flights on a route.

    Prototype: tries a lightweight HTTP scrape first; falls back to mock data.

    Args:
        origin:      IATA airport code or city name (e.g. "EDI", "Edinburgh").
        destination: IATA airport code or city name (e.g. "LHR", "London").
        date:        Departure date in YYYY-MM-DD format.
        sort_by:     Ranking preference — "cost", "comfort", or "time".
        num_results: Maximum number of results to return (default 5).

    Returns:
        List of flight dicts, each with: id, airline, origin, destination,
        departure, arrival, duration_min, stops, price_gbp, seats_remaining.
    """
    flights = await _fetch_flights(origin, destination, date)
    flights = _sort(flights, sort_by)
    return flights[:num_results]


async def search_flights_mock(
    origin: str,
    destination: str,
    date: str,
    sort_by: str = "cost",
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Deterministic seeded results only — no network, no cache. For offline/test providers."""
    flights = _seeded_flights(origin, destination, date)
    flights = _sort(flights, sort_by)
    return flights[:num_results]


async def _fetch_flights(origin: str, destination: str, date: str) -> list[dict[str, Any]]:
    """Cached wrapper around the live fetch — reused across identical route+date searches."""
    key = flight_cache_key(fingerprint(origin.lower(), destination.lower(), date))
    return await get_or_set(
        key, _CACHE_TTL_SECONDS, lambda: _fetch_flights_live(origin, destination, date)
    )


async def _fetch_flights_live(origin: str, destination: str, date: str) -> list[dict[str, Any]]:
    """Attempt real HTTP fetch; fall back to mock data on any failure."""
    try:
        import httpx  # optional dep — in 'conversation' extra

        # Prototype target: AviationStack free tier (no key needed for basic routes)
        # Replace with a real API or scraping target in production.
        url = (
            "https://api.aviationstack.com/v1/flights"
            f"?dep_iata={origin.upper()[:3]}&arr_iata={destination.upper()[:3]}"
            f"&flight_date={date}&limit={10}"
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    return _normalise_aviationstack(data)
    except Exception as exc:
        logger.debug("Flight HTTP fetch failed (%s) — using mock data", exc)

    return _seeded_flights(origin, destination, date)


def _normalise_aviationstack(raw: list[dict]) -> list[dict[str, Any]]:
    """Map AviationStack response fields to our internal flight schema."""
    results = []
    for f in raw:
        dep = f.get("departure", {})
        arr = f.get("arrival", {})
        results.append({
            "id": f.get("flight", {}).get("iata", "UNK"),
            "airline": f.get("airline", {}).get("name", "Unknown"),
            "aircraft": f.get("aircraft", {}).get("iata", "Unknown"),
            "origin": dep.get("iata", ""),
            "destination": arr.get("iata", ""),
            "date": (dep.get("scheduled") or "")[:10],
            "departure": dep.get("scheduled", ""),
            "arrival": arr.get("scheduled", ""),
            "duration_min": None,   # not provided by this endpoint
            "stops": 0,
            "price_gbp": None,      # AviationStack free tier has no pricing
            "cabin": "economy",
            "seats_remaining": None,
        })
    return results


def _sort(flights: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    """Sort flight results by the requested priority."""
    if sort_by == "cost":
        return sorted(flights, key=lambda f: f.get("price_gbp") or float("inf"))
    if sort_by == "comfort":
        # Fewest stops first; then cheapest within same stop count
        return sorted(flights, key=lambda f: (f.get("stops", 99), f.get("price_gbp") or float("inf")))
    if sort_by == "time":
        return sorted(flights, key=lambda f: f.get("duration_min") or float("inf"))
    return flights


# ---------------------------------------------------------------------------
# OpenAI function-calling schema (registered with ToolRegistry)
# ---------------------------------------------------------------------------

SEARCH_FLIGHTS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "search_flights",
        "description": (
            "Search for available flights between two airports on a given date. "
            "Returns a ranked list of flight options with price, duration, and stops."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Departure airport IATA code or city name (e.g. 'EDI', 'Edinburgh')",
                },
                "destination": {
                    "type": "string",
                    "description": "Arrival airport IATA code or city name (e.g. 'LHR', 'London')",
                },
                "date": {
                    "type": "string",
                    "description": "Departure date in YYYY-MM-DD format",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["cost", "comfort", "time"],
                    "description": "Ranking preference. Default is 'cost'.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5)",
                },
            },
            "required": ["origin", "destination", "date"],
        },
    },
}
