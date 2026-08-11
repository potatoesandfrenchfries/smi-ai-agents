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
    SMI_WEATHER_PROVIDER    open_meteo | mock       (default: open_meteo)
    SMI_MAPS_PROVIDER       nominatim | mock        (default: nominatim)
    SMI_BUDGET_PROVIDER     default | mock          (default: default)
"""

from __future__ import annotations

import os

from smi_agent.providers.budget import DefaultBudgetProvider, MockBudgetProvider
from smi_agent.providers.flight import AviationStackFlightProvider, MockFlightProvider
from smi_agent.providers.hotel import MockHotelProvider, OverpassHotelProvider
from smi_agent.providers.interface import (
    BudgetProvider,
    FlightProvider,
    HotelProvider,
    MapsProvider,
    RestaurantProvider,
    WeatherProvider,
)
from smi_agent.providers.maps import MockMapsProvider, NominatimMapsProvider
from smi_agent.providers.restaurant import MockRestaurantProvider, OverpassRestaurantProvider
from smi_agent.providers.weather import MockWeatherProvider, OpenMeteoWeatherProvider

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
_WEATHER_PROVIDERS: dict[str, type[WeatherProvider]] = {
    "open_meteo": OpenMeteoWeatherProvider,
    "mock": MockWeatherProvider,
}
_MAPS_PROVIDERS: dict[str, type[MapsProvider]] = {
    "nominatim": NominatimMapsProvider,
    "mock": MockMapsProvider,
}
_BUDGET_PROVIDERS: dict[str, type[BudgetProvider]] = {
    "default": DefaultBudgetProvider,
    "mock": MockBudgetProvider,
}

_flight_provider: FlightProvider | None = None
_hotel_provider: HotelProvider | None = None
_restaurant_provider: RestaurantProvider | None = None
_weather_provider: WeatherProvider | None = None
_maps_provider: MapsProvider | None = None
_budget_provider: BudgetProvider | None = None


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


def get_weather_provider() -> WeatherProvider:
    global _weather_provider
    if _weather_provider is None:
        name = os.environ.get("SMI_WEATHER_PROVIDER", "open_meteo")
        if name not in _WEATHER_PROVIDERS:
            raise ValueError(f"Unknown SMI_WEATHER_PROVIDER={name!r}. Allowed: {sorted(_WEATHER_PROVIDERS)}")
        _weather_provider = _WEATHER_PROVIDERS[name]()
    return _weather_provider


def get_maps_provider() -> MapsProvider:
    global _maps_provider
    if _maps_provider is None:
        name = os.environ.get("SMI_MAPS_PROVIDER", "nominatim")
        if name not in _MAPS_PROVIDERS:
            raise ValueError(f"Unknown SMI_MAPS_PROVIDER={name!r}. Allowed: {sorted(_MAPS_PROVIDERS)}")
        _maps_provider = _MAPS_PROVIDERS[name]()
    return _maps_provider


def get_budget_provider() -> BudgetProvider:
    global _budget_provider
    if _budget_provider is None:
        name = os.environ.get("SMI_BUDGET_PROVIDER", "default")
        if name not in _BUDGET_PROVIDERS:
            raise ValueError(f"Unknown SMI_BUDGET_PROVIDER={name!r}. Allowed: {sorted(_BUDGET_PROVIDERS)}")
        _budget_provider = _BUDGET_PROVIDERS[name]()
    return _budget_provider
