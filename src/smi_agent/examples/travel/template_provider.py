"""Travel domain template provider — generates AI insights for entity pages."""

from __future__ import annotations

import logging
from typing import Any

from smi_agent.domain.interfaces import TemplateProvider

logger = logging.getLogger(__name__)


class TravelTemplateProvider(TemplateProvider):
    """Generates AI-powered insight templates for travel entity pages.

    When a user opens a flight, hotel, or booking detail page, this provider
    fetches the relevant data and generates a structured summary with
    suggestions and context-aware chips.
    """

    async def generate_template(
        self,
        pg_executor: Any,
        conversation_id: str,
        context_type: str | None,
        entity_id: str | None,
        label: str | None,
        tenant_id: str,
        on_step: Any = None,
    ) -> dict[str, Any]:
        # Handle list pages (no specific entity)
        if label and not entity_id:
            return await self._generate_list_template(
                pg_executor, conversation_id, context_type, label, tenant_id, on_step
            )

        # Handle detail pages (specific entity)
        if context_type == "flight" and entity_id:
            return await self._generate_flight_template(
                pg_executor, conversation_id, entity_id, tenant_id, on_step
            )

        if context_type == "hotel" and entity_id:
            return await self._generate_hotel_template(
                pg_executor, conversation_id, entity_id, tenant_id, on_step
            )

        if context_type == "booking" and entity_id:
            return await self._generate_booking_template(
                pg_executor, conversation_id, entity_id, tenant_id, on_step
            )

        # Generic fallback
        return {
            "summary": "",
            "recommendation": "",
            "synthesisBullets": [],
            "contextChips": [],
            "suggestions": [
                "Show me upcoming flights",
                "What are the top-rated hotels?",
                "Show recent bookings",
            ],
            "inputPlaceholder": f"Ask about {label or 'travel'}...",
        }

    async def _generate_list_template(
        self, pg_executor, conversation_id, context_type, label, tenant_id, on_step
    ) -> dict:
        bullets = []
        chips = []
        summary = ""
        suggestions = []

        if context_type == "flight":
            if on_step:
                await on_step("Fetching flight statistics...")
            try:
                stats = await pg_executor.run("fetch_flight_stats", tenant_id)
                if stats:
                    s = stats[0]
                    summary = f"{s['total']} flights tracked: {s['scheduled']} scheduled, {s['in_air']} in air"
                    bullets = [
                        f"{s['scheduled']} flights scheduled for departure",
                        f"{s['in_air']} flights currently in the air",
                        f"{s['delayed']} flights delayed — avg price ${s['avg_price']:.0f}" if s['delayed'] else "",
                        f"Price range: ${s['min_price']:.0f} to ${s['max_price']:.0f}" if s['min_price'] else "",
                    ]
                    bullets = [b for b in bullets if b]
                    chips = [{"label": "Status Breakdown", "icon": "chart"}, {"label": "Price Trends", "icon": "dollar"}]
                suggestions = [
                    "Show me the next departing flights",
                    "Which airlines have the best on-time performance?",
                    "Find the cheapest flights this week",
                ]
            except Exception:
                logger.debug("Flight stats fetch failed for template")

        elif context_type == "hotel":
            suggestions = [
                "Show me top-rated hotels",
                "Find hotels under $200/night",
                "Which hotels have availability this weekend?",
            ]

        elif context_type == "booking":
            if on_step:
                await on_step("Fetching booking statistics...")
            try:
                stats = await pg_executor.run("fetch_booking_stats", tenant_id)
                if stats:
                    s = stats[0]
                    summary = f"{s['total']} total bookings — {s['confirmed']} confirmed, ${s['total_revenue']:.0f} total revenue" if s['total_revenue'] else f"{s['total']} total bookings"
                    bullets = [
                        f"{s['confirmed']} confirmed bookings",
                        f"{s['pending']} pending confirmation",
                        f"Average booking value: ${s['avg_booking_value']:.0f}" if s['avg_booking_value'] else "",
                    ]
                    bullets = [b for b in bullets if b]
                    chips = [{"label": "Revenue", "icon": "dollar"}, {"label": "Status", "icon": "list"}]
                suggestions = [
                    "Show recent bookings",
                    "Which bookings need attention?",
                    "Find bookings by traveler name",
                ]
            except Exception:
                logger.debug("Booking stats fetch failed for template")

        return {
            "summary": summary,
            "recommendation": "",
            "synthesisBullets": [{"text": b} for b in bullets],
            "contextChips": chips,
            "suggestions": suggestions,
            "inputPlaceholder": f"Ask about {label or 'travel'}...",
        }

    async def _generate_flight_template(self, pg_executor, conversation_id, flight_id, tenant_id, on_step) -> dict:
        if on_step:
            await on_step("Fetching flight details...")
        try:
            rows = await pg_executor.run("fetch_flight_detail", flight_id, tenant_id)
            if rows:
                f = rows[0]
                return {
                    "summary": f"Flight {f['flight_number']} from {f['departure_city']} ({f['departure_code']}) to {f['arrival_city']} ({f['arrival_code']}). Operated by {f['airline_name']}.",
                    "recommendation": f"Status: {f['status']}. {'On-time rate: ' + str(f['on_time_percentage']) + '%' if f['on_time_percentage'] else ''}",
                    "synthesisBullets": [
                        {"text": f"Departure: {f['scheduled_departure']} from {f['departure_code']}"},
                        {"text": f"Arrival: {f['scheduled_arrival']} at {f['arrival_code']}"},
                        {"text": f"Price: {f['price']} {f['currency']} — {f['available_seats']} seats available"},
                        {"text": f"Aircraft: {f['aircraft_type']} — Duration: {f['duration_minutes']} min"},
                    ],
                    "contextChips": [
                        {"label": f"${f['price']}", "icon": "dollar"},
                        {"label": f["status"], "icon": "plane"},
                        {"label": f["airline_name"], "icon": "building"},
                    ],
                    "suggestions": [
                        "Show me alternative flights on this route",
                        "What hotels are near the arrival airport?",
                        "Check the on-time history for this route",
                    ],
                    "inputPlaceholder": f"Ask about flight {f['flight_number']}...",
                }
        except Exception:
            logger.debug("Flight detail fetch failed for template", exc_info=True)

        return self._fallback_template()

    async def _generate_hotel_template(self, pg_executor, conversation_id, hotel_id, tenant_id, on_step) -> dict:
        if on_step:
            await on_step("Fetching hotel details...")
        try:
            rows = await pg_executor.run("fetch_hotel_detail", hotel_id, tenant_id)
            if rows:
                h = rows[0]
                amenities = h.get("amenities", []) or []
                return {
                    "summary": f"{h['name']} — {h['star_rating']}-star hotel in {h['city']}, {h['country']}. Rated {h['rating']}/5 from {h['review_count']} reviews.",
                    "recommendation": f"From {h['price_per_night']} {h['currency']}/night. {'Free WiFi' if h['free_wifi'] else 'WiFi available'}.",
                    "synthesisBullets": [
                        {"text": f"Rating: {'★' * h['star_rating']} ({h['rating']}/5 from {h['review_count']} reviews)"},
                        {"text": f"Price: {h['price_per_night']} {h['currency']} per night — {h['available_rooms']} rooms available"},
                        {"text": f"Location: {h['distance_to_city_center_km']}km from city center"},
                        {"text": f"Amenities: {', '.join(amenities[:5])}" if amenities else ""},
                    ],
                    "contextChips": [
                        {"label": f"{h['star_rating']}★", "icon": "star"},
                        {"label": f"${h['price_per_night']}/night", "icon": "dollar"},
                        {"label": h["city"], "icon": "map-pin"},
                    ],
                    "suggestions": [
                        f"How far is {h['name']} from the airport?",
                        "Show me similar hotels nearby",
                        "What are the check-in and check-out times?",
                    ],
                    "inputPlaceholder": f"Ask about {h['name']}...",
                }
        except Exception:
            logger.debug("Hotel detail fetch failed for template", exc_info=True)
        return self._fallback_template()

    async def _generate_booking_template(self, pg_executor, conversation_id, booking_id, tenant_id, on_step) -> dict:
        if on_step:
            await on_step("Fetching booking details...")
        try:
            rows = await pg_executor.run("fetch_booking_detail", booking_id, tenant_id)
            if rows:
                b = rows[0]
                return {
                    "summary": f"Booking {b['booking_reference']} for {b['first_name']} {b['last_name']}. Status: {b['status']}.",
                    "recommendation": f"Total: {b['total_price']} {b['currency']}. Payment: {b['payment_status']}.",
                    "synthesisBullets": [
                        {"text": f"Traveler: {b['first_name']} {b['last_name']} ({b['email']})"},
                        {"text": f"Class: {b['seat_class']} — Baggage: {'Included' if b['baggage_included'] else 'Not included'}"},
                        {"text": f"Check-in: {b['check_in_date']} — Check-out: {b['check_out_date']}"},
                        {"text": f"Cancellation: {b['cancellation_policy']}"},
                    ],
                    "contextChips": [
                        {"label": b["status"], "icon": "tag"},
                        {"label": f"{b['total_price']} {b['currency']}", "icon": "dollar"},
                        {"label": b["seat_class"], "icon": "star"},
                    ],
                    "suggestions": [
                        "What's the cancellation policy for this booking?",
                        "Can I change the seat assignment?",
                        "Show me similar bookings",
                    ],
                    "inputPlaceholder": f"Ask about booking {b['booking_reference']}...",
                }
        except Exception:
            logger.debug("Booking detail fetch failed for template", exc_info=True)
        return self._fallback_template()

    @staticmethod
    def _fallback_template() -> dict:
        return {
            "summary": "",
            "recommendation": "",
            "synthesisBullets": [],
            "contextChips": [],
            "suggestions": [],
            "inputPlaceholder": "Ask about this...",
        }