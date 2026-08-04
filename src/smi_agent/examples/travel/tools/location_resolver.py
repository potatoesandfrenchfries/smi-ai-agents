"""Location resolver — normalises any form of a location (IATA code, city
name, historical/alternate name) to every other known form.

Why this exists
────────────────
parse_intent resolves a traveler's destination to an IATA code (e.g.
"Chennai" → "MAA") because that's what flight search needs. But the
OpenStreetMap-backed hotel/restaurant/attraction searches need the real place
name ("Chennai"), not an airport code — Overpass has no idea what "MAA"
means. Before this resolver existed, constraints["destination"] was passed
straight through to every search function, so the Overpass-based ones only
ever worked when the destination happened to already look like a place name.

This module is the single source of truth other agents/tools call to get
"the form they need" instead of each one guessing or duplicating its own
city table (flight_scraper.py, hotel_scraper.py, restaurant_scraper.py, and
attraction_scraper.py each had their own slightly different city lists before
this existed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CityInfo:
    canonical: str            # lowercase, matches Overpass area[name=...] lookups
    display_name: str         # Title Case, for human-facing text
    iata: str
    aliases: tuple[str, ...] = ()


_CITIES: tuple[CityInfo, ...] = (
    CityInfo("chennai", "Chennai", "MAA", aliases=("madras",)),
    CityInfo("mumbai", "Mumbai", "BOM", aliases=("bombay",)),
    CityInfo("delhi", "Delhi", "DEL", aliases=("new delhi",)),
    CityInfo("hyderabad", "Hyderabad", "HYD"),
    CityInfo("bengaluru", "Bengaluru", "BLR", aliases=("bangalore",)),
    CityInfo("lucknow", "Lucknow", "LKO"),
    CityInfo("patna", "Patna", "PAT"),
    CityInfo("kolkata", "Kolkata", "CCU", aliases=("calcutta",)),
    CityInfo("ahmedabad", "Ahmedabad", "AMD"),
    CityInfo("pune", "Pune", "PNQ"),
    CityInfo("goa", "Goa", "GOI"),
    CityInfo("kochi", "Kochi", "COK", aliases=("cochin",)),
    CityInfo("london", "London", "LHR"),
    CityInfo("edinburgh", "Edinburgh", "EDI"),
    CityInfo("paris", "Paris", "CDG"),
    CityInfo("amsterdam", "Amsterdam", "AMS"),
    CityInfo("berlin", "Berlin", "BER"),
    CityInfo("madrid", "Madrid", "MAD"),
    CityInfo("rome", "Rome", "FCO"),
    CityInfo("new york", "New York", "JFK", aliases=("nyc", "new york city")),
    CityInfo("dubai", "Dubai", "DXB"),
)

_BY_CANONICAL: dict[str, CityInfo] = {c.canonical: c for c in _CITIES}
_BY_ALIAS: dict[str, CityInfo] = {alias: c for c in _CITIES for alias in c.aliases}
_BY_IATA: dict[str, CityInfo] = {c.iata: c for c in _CITIES}


def resolve_location(query: str) -> dict[str, Any]:
    """Resolve any known form of a location to every other form.

    Accepts an IATA code ("MAA"), a canonical city name ("chennai", any
    case), or a historical/alternate name ("Madras"). Unknown input degrades
    gracefully — city_name and iata both fall back to the original string so
    a caller can still use the result rather than crashing on a KeyError.

    Returns:
        {
          "query": <original input>,
          "matched": bool,
          "city_name": <lowercase place name — feed this to Overpass-based
                        hotel/restaurant/attraction search>,
          "iata": <3-letter code — feed this to flight search>,
          "display_name": <Title Case, for human-facing text>,
          "aliases": [<other known names>],
        }
    """
    if not query or not query.strip():
        return {
            "query": query, "matched": False,
            "city_name": query, "iata": query,
            "display_name": query, "aliases": [],
        }

    stripped = query.strip()
    info = (
        _BY_CANONICAL.get(stripped.lower())
        or _BY_ALIAS.get(stripped.lower())
        or _BY_IATA.get(stripped.upper())
    )

    if info is None:
        return {
            "query": query, "matched": False,
            "city_name": stripped, "iata": stripped.upper(),
            "display_name": stripped.title(), "aliases": [],
        }

    return {
        "query": query, "matched": True,
        "city_name": info.canonical, "iata": info.iata,
        "display_name": info.display_name, "aliases": list(info.aliases),
    }


def find_locations_in_text(text: str) -> list[str]:
    """Scan free text for known city names, aliases, or IATA codes, returning
    each match's IATA code in the order it first appears (deduplicated).

    For callers that need to pull "origin" and "destination" out of a loose
    sentence (e.g. FlightSpecialist._parse_query on a supervisor-composed
    query like "Flights EDI to CDG on 2026-09-10") without an LLM call.
    Matching is on word boundaries against this module's shared _CITIES
    table, so it recognises actual city names ("Edinburgh", "Paris") and
    known IATA codes — unlike a bare "grab any 3-letter ALL-CAPS word" scan,
    it won't false-positive on ordinary words like "the" or "top", and it
    won't silently fail on a city name that isn't already an airport code.
    """
    if not text:
        return []

    matches: list[tuple[int, str]] = []

    names_to_iata: dict[str, str] = {c.canonical: c.iata for c in _CITIES}
    for c in _CITIES:
        for alias in c.aliases:
            names_to_iata[alias] = c.iata

    lowered = text.lower()
    for name, iata in names_to_iata.items():
        for m in re.finditer(rf"\b{re.escape(name)}\b", lowered):
            matches.append((m.start(), iata))

    # Explicit IATA codes as their own token — validated against known codes
    # only, so this can't false-positive the way "any 3-letter word" did.
    for m in re.finditer(r"\b[A-Za-z]{3}\b", text):
        code = m.group(0).upper()
        if code in _BY_IATA:
            matches.append((m.start(), code))

    matches.sort(key=lambda pair: pair[0])

    ordered: list[str] = []
    for _, iata in matches:
        if iata not in ordered:
            ordered.append(iata)
    return ordered


def to_iata(query: str) -> str:
    """Shortcut for callers that only need the IATA code (flight search)."""
    return resolve_location(query)["iata"]


def to_city_name(query: str) -> str:
    """Shortcut for callers that need a real place name, not an airport code
    (Overpass-backed hotel/restaurant/attraction search).
    """
    return resolve_location(query)["city_name"]


def to_display_name(query: str) -> str:
    """Shortcut for callers building human-facing text."""
    return resolve_location(query)["display_name"]


RESOLVE_LOCATION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "resolve_location",
        "description": (
            "Resolve any form of a location — IATA airport code, city name, or "
            "historical/alternate name (e.g. 'Madras' for Chennai, 'Bombay' for "
            "Mumbai) — to its full set of known forms: canonical city name, IATA "
            "code, display name, and known aliases. Call this before any "
            "location-based search so the right form reaches the right tool: "
            "IATA codes for flight search, real place names for OpenStreetMap-"
            "backed hotel/restaurant/attraction search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Any known form of a location, e.g. 'Chennai', 'MAA', or 'Madras'",
                },
            },
            "required": ["query"],
        },
    },
}
