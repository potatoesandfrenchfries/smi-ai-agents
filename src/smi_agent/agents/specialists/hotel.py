"""HotelSpecialist — searches for hotels and returns ranked options."""

from __future__ import annotations

import json
import logging
from typing import Any

from smi_agent.agents.response import ResponseType, StructuredResponse
from smi_agent.agents.specialists.base import BaseSpecialist
from smi_agent.streaming import StepEmitter

logger = logging.getLogger(__name__)


class HotelSpecialist(BaseSpecialist):
    """Finds and ranks available hotels for a given location and stay period."""

    @property
    def name(self) -> str:
        return "hotel"

    @property
    def description(self) -> str:
        return (
            "Search for available hotels at a location for given check-in and check-out dates. "
            "Returns the best options ranked by price, rating, or proximity to the city centre."
        )

    async def run(
        self,
        query: str,
        context: dict[str, Any],
        step_emitter: StepEmitter,
    ) -> StructuredResponse:
        from smi_agent.examples.travel.tools.hotel_scraper import search_hotels

        location = context.get("location", "")
        check_in = context.get("check_in", "")
        check_out = context.get("check_out", "")
        sort_by = context.get("sort_by", "rating")

        if not (location and check_in and check_out):
            location, check_in, check_out, sort_by = _parse_query(query, sort_by)

        await step_emitter.emit(
            "hotel_search",
            "in_progress",
            f"Searching hotels in {location} ({check_in} to {check_out}, sort: {sort_by})",
        )

        try:
            hotels = await search_hotels(
                location=location,
                check_in=check_in,
                check_out=check_out,
                sort_by=sort_by,
            )
        except Exception as exc:
            logger.exception("HotelSpecialist: search_hotels failed")
            await step_emitter.emit("hotel_search", "failed", str(exc))
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.error,
                status="error",
                blocks=[{"type": "error", "message": f"Hotel search failed: {exc}"}],
            )

        await step_emitter.emit("hotel_search", "completed", f"Found {len(hotels)} hotel(s)")

        if not hotels:
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.entity_list,
                status="no_data",
                blocks=[{
                    "type": "text",
                    "content": f"No hotels found in {location} for {check_in} to {check_out}. "
                               "Try a nearby area or different dates.",
                }],
            )

        nights = hotels[0].get("nights", 1)
        table_rows = [
            {
                "Hotel": h["name"],
                "Stars": "★" * h["stars"],
                "Rating": f"{h['rating']}/10 ({h['review_count']} reviews)",
                "Per night": f"£{h['price_per_night_gbp']:.2f}",
                f"Total ({nights}n)": f"£{h['total_price_gbp']:.2f}",
                "Distance": f"{h['distance_from_centre_km']} km",
                "Amenities": ", ".join(h["amenities"][:3]),
            }
            for h in hotels
        ]

        best = hotels[0]
        summary = (
            f"Top pick ({sort_by}): {best['name']} — {best['stars']}★, "
            f"rated {best['rating']}/10, £{best['price_per_night_gbp']:.2f}/night "
            f"(£{best['total_price_gbp']:.2f} total for {nights} night(s)), "
            f"{best['distance_from_centre_km']} km from centre."
        )

        return StructuredResponse(
            agent=self.name,
            responseType=ResponseType.entity_list,
            status="success",
            blocks=[
                {"type": "text", "content": summary},
                {"type": "table", "columns": list(table_rows[0].keys()), "rows": table_rows},
            ],
            payload={"hotels": hotels, "sort_by": sort_by, "nights": nights},
            followUps=[
                "Show me hotels with a pool included",
                "Are there cheaper options further from the centre?",
                f"What's the best 5-star hotel in {location}?",
            ],
            sources=[{"tool": "search_hotels", "query": json.dumps(
                {"location": location, "check_in": check_in, "check_out": check_out}
            )}],
        )


def _parse_query(query: str, default_sort: str) -> tuple[str, str, str, str]:
    import re
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", query)
    check_in = dates[0] if len(dates) > 0 else "2026-08-01"
    check_out = dates[1] if len(dates) > 1 else "2026-08-05"

    sort_by = default_sort
    if "cheap" in query.lower() or "price" in query.lower():
        sort_by = "price"
    elif "close" in query.lower() or "central" in query.lower() or "near" in query.lower():
        sort_by = "proximity"
    elif "rating" in query.lower() or "best" in query.lower():
        sort_by = "rating"

    # Use everything before any date as the location (rough heuristic)
    location = re.split(r"\d{4}-\d{2}-\d{2}", query)[0].strip().rstrip(" ,.-") or "Unknown"
    return location, check_in, check_out, sort_by
