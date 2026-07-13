"""Selects the active Flight/Hotel/Restaurant provider.

Callers (Temporal activities, the itinerary graph, specialist agents) go
through get_flight_provider()/get_hotel_provider()/get_restaurant_provider()
instead of importing a concrete data source. Switching providers — or
plugging in a brand new one — is:

  1. Write a class implementing FlightProvider/HotelProvider/RestaurantProvider
     (see providers/flight.py, hotel.py, restaurant.py for examples).
  2. Add it to the relevant _PROVIDERS dict below.
  3. Set the matching env var (e.g. SMI_FLIGHT_PROVIDER=skyscanner).

No call site changes.

Env vars (default shown):
    SMI_FLIGHT_PROVIDER     aviationstack | mock   (default: aviationstack)
    SMI_HOTEL_PROVIDER      overpass | mock         (default: overpass)
    SMI_RESTAURANT_PROVIDER overpass | mock         (default: overpass)
"""

from __future__ import annotations

import os

from smi_agent.providers.flight import AviationStackFlightProvider, MockFlightProvider
from smi_agent.providers.hotel import MockHotelProvider, OverpassHotelProvider
from smi_agent.providers.interface import FlightProvider, HotelProvider, RestaurantProvider
from smi_agent.providers.restaurant import MockRestaurantProvider, OverpassRestaurantProvider

_FLIGHT_PROVIDERS: dict[str, type[FlightProvider]] = {
    "aviationstack": AviationStackFlightProvider,
    "mock": MockFlightProvider,
}
_HOTEL_PROVIDERS: dict[str, type[HotelProvider]] = {
    "overpass": OverpassHotelProvider,
    "mock": MockHotelProvider,
}
_RESTAURANT_PROVIDERS: dict[str, type[RestaurantProvider]] = {
    "overpass": OverpassRestaurantProvider,
    "mock": MockRestaurantProvider,
}

_flight_provider: FlightProvider | None = None
_hotel_provider: HotelProvider | None = None
_restaurant_provider: RestaurantProvider | None = None


def get_flight_provider() -> FlightProvider:
    global _flight_provider
    if _flight_provider is None:
        name = os.environ.get("SMI_FLIGHT_PROVIDER", "aviationstack")
        if name not in _FLIGHT_PROVIDERS:
            raise ValueError(f"Unknown SMI_FLIGHT_PROVIDER={name!r}. Allowed: {sorted(_FLIGHT_PROVIDERS)}")
        _flight_provider = _FLIGHT_PROVIDERS[name]()
    return _flight_provider


def get_hotel_provider() -> HotelProvider:
    global _hotel_provider
    if _hotel_provider is None:
        name = os.environ.get("SMI_HOTEL_PROVIDER", "overpass")
        if name not in _HOTEL_PROVIDERS:
            raise ValueError(f"Unknown SMI_HOTEL_PROVIDER={name!r}. Allowed: {sorted(_HOTEL_PROVIDERS)}")
        _hotel_provider = _HOTEL_PROVIDERS[name]()
    return _hotel_provider


def get_restaurant_provider() -> RestaurantProvider:
    global _restaurant_provider
    if _restaurant_provider is None:
        name = os.environ.get("SMI_RESTAURANT_PROVIDER", "overpass")
        if name not in _RESTAURANT_PROVIDERS:
            raise ValueError(
                f"Unknown SMI_RESTAURANT_PROVIDER={name!r}. Allowed: {sorted(_RESTAURANT_PROVIDERS)}"
            )
        _restaurant_provider = _RESTAURANT_PROVIDERS[name]()
    return _restaurant_provider
