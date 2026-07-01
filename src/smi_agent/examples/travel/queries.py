"""Travel domain Postgres queries and tool definitions.

Provides domain-specific SQL queries for flights, hotels, bookings,
and travel analytics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smi_agent.domain.interfaces import QueryProvider


@dataclass(frozen=True)
class SqlQuery:
    name: str
    sql: str
    is_read: bool
    allowed_agents: list[str] = field(default_factory=lambda: ["*"])


# ── Flight queries ────────────────────────────────────────────────────────────

FETCH_FLIGHT_DETAIL = SqlQuery(
    name="fetch_flight_detail",
    sql="""\
SELECT f.id, f.flight_number, f.display_id, f.airline_id, a.name AS airline_name,
       f.departure_airport_id, da.code AS departure_code, da.city AS departure_city,
       f.arrival_airport_id, aa.code AS arrival_code, aa.city AS arrival_city,
       f.scheduled_departure, f.scheduled_arrival, f.status,
       f.price, f.currency, f.available_seats, f.aircraft_type,
       f.duration_minutes, f.has_stopover, f.on_time_percentage
FROM flights f
JOIN airlines a ON a.id = f.airline_id
JOIN airports da ON da.id = f.departure_airport_id
JOIN airports aa ON aa.id = f.arrival_airport_id
WHERE f.id = $1 AND f.tenant_id = $2
""",
    is_read=True,
)

FETCH_TOP_FLIGHTS = SqlQuery(
    name="fetch_top_flights",
    sql="""\
SELECT f.id, f.display_id, f.flight_number, f.airline_id, a.name AS airline_name,
       f.departure_airport_id, da.code AS departure_code, da.city AS departure_city,
       f.arrival_airport_id, aa.code AS arrival_code, aa.city AS arrival_city,
       f.scheduled_departure, f.scheduled_arrival, f.status,
       f.price, f.currency, f.available_seats, f.duration_minutes,
       f.on_time_percentage
FROM flights f
JOIN airlines a ON a.id = f.airline_id
JOIN airports da ON da.id = f.departure_airport_id
JOIN airports aa ON aa.id = f.arrival_airport_id
WHERE f.tenant_id = $1 AND f.status != 'CANCELLED'
ORDER BY f.scheduled_departure ASC
LIMIT $2
""",
    is_read=True,
)

FETCH_FLIGHT_STATS = SqlQuery(
    name="fetch_flight_stats",
    sql="""\
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE status = 'SCHEDULED') AS scheduled,
    count(*) FILTER (WHERE status = 'BOARDING') AS boarding,
    count(*) FILTER (WHERE status = 'IN_AIR') AS in_air,
    count(*) FILTER (WHERE status = 'LANDED') AS landed,
    count(*) FILTER (WHERE status = 'DELAYED') AS delayed,
    count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
    avg(price) AS avg_price,
    min(price) AS min_price,
    max(price) AS max_price
FROM flights
WHERE tenant_id = $1
""",
    is_read=True,
)

# ── Hotel queries ─────────────────────────────────────────────────────────────

FETCH_HOTEL_DETAIL = SqlQuery(
    name="fetch_hotel_detail",
    sql="""\
SELECT h.id, h.display_id, h.name, h.star_rating, h.chain_name,
       h.city, h.country, h.address,
       h.price_per_night, h.currency, h.available_rooms,
       h.amenities, h.rating, h.review_count,
       h.distance_to_city_center_km, h.has_pool, h.has_gym, h.free_wifi
FROM hotels h
WHERE h.id = $1 AND h.tenant_id = $2
""",
    is_read=True,
)

FETCH_TOP_HOTELS = SqlQuery(
    name="fetch_top_hotels",
    sql="""\
SELECT h.id, h.display_id, h.name, h.star_rating, h.city, h.country,
       h.price_per_night, h.currency, h.rating, h.review_count,
       h.available_rooms
FROM hotels h
WHERE h.tenant_id = $1 AND h.available_rooms > 0
ORDER BY h.rating DESC, h.review_count DESC
LIMIT $2
""",
    is_read=True,

)

# ── Booking queries ────────────────────────────────────────────────────────────

FETCH_BOOKING_DETAIL = SqlQuery(
    name="fetch_booking_detail",
    sql="""\
SELECT b.id, b.display_id, b.booking_reference, b.status,
       b.traveler_id, t.first_name, t.last_name, t.email,
       b.flight_id, b.hotel_id,
       b.total_price, b.currency, b.payment_status,
       b.booked_at, b.updated_at, b.check_in_date, b.check_out_date,
       b.seat_class, b.baggage_included, b.meal_included,
       b.cancellation_policy, b.refundable_until
FROM bookings b
JOIN travelers t ON t.id = b.traveler_id
WHERE b.id = $1 AND b.tenant_id = $2
""",
    is_read=True,
)

FETCH_BOOKING_STATS = SqlQuery(
    name="fetch_booking_stats",
    sql="""\
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE status = 'CONFIRMED') AS confirmed,
    count(*) FILTER (WHERE status = 'PENDING') AS pending,
    count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
    count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
    sum(total_price) AS total_revenue,
    avg(total_price) AS avg_booking_value
FROM bookings
WHERE tenant_id = $1
""",
    is_read=True,
)


# ── Query provider ─────────────────────────────────────────────────────────────


class TravelQueryProvider(QueryProvider):
    """Query provider for the travel domain."""

    @property
    def read_queries(self) -> dict[str, Any]:
        return {
            "fetch_flight_detail": FETCH_FLIGHT_DETAIL,
            "fetch_top_flights": FETCH_TOP_FLIGHTS,
            "fetch_flight_stats": FETCH_FLIGHT_STATS,
            "fetch_hotel_detail": FETCH_HOTEL_DETAIL,
            "fetch_top_hotels": FETCH_TOP_HOTELS,
            "fetch_booking_detail": FETCH_BOOKING_DETAIL,
            "fetch_booking_stats": FETCH_BOOKING_STATS,
        }

    @property
    def write_queries(self) -> dict[str, Any]:
        return {}

    def tool_definitions(self) -> dict[str, dict[str, Any]]:
        return {
            "fetch_flight_detail": {
                "description": "Fetch full flight details: route, airline, status, price, aircraft, on-time performance",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "flight_id": {"type": "string", "description": "Flight UUID"},
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                    },
                    "required": ["flight_id", "tenant_id"],
                },
            },
            "fetch_top_flights": {
                "description": "Fetch upcoming flights ordered by departure time. Returns flight number, route, airline, status, price.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                        "limit": {"type": "integer", "description": "Number of flights to return (default 10, max 50)", "default": 10, "maximum": 50},
                    },
                    "required": ["tenant_id"],
                },
            },
            "fetch_flight_stats": {
                "description": "Fetch aggregate flight statistics: counts by status, price range",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                    },
                    "required": ["tenant_id"],
                },
            },
            "fetch_hotel_detail": {
                "description": "Fetch full hotel details: star rating, price, amenities, ratings, location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hotel_id": {"type": "string", "description": "Hotel UUID"},
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                    },
                    "required": ["hotel_id", "tenant_id"],
                },
            },
            "fetch_top_hotels": {
                "description": "Fetch top-rated hotels with availability. Returns name, city, star rating, price, rating, review count.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                        "limit": {"type": "integer", "description": "Number of hotels to return (default 10, max 25)", "default": 10, "maximum": 25},
                    },
                    "required": ["tenant_id"],
                },
            },
            "fetch_booking_detail": {
                "description": "Fetch full booking details: traveler, flights, hotels, payment status, seat class, cancellation policy",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "booking_id": {"type": "string", "description": "Booking UUID"},
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                    },
                    "required": ["booking_id", "tenant_id"],
                },
            },
            "fetch_booking_stats": {
                "description": "Fetch aggregate booking statistics: counts by status, revenue totals, average booking value",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "Tenant UUID"},
                    },
                    "required": ["tenant_id"],
                },
            },
        }