"""RestaurantSpecialist — searches for restaurants and returns ranked options."""

from __future__ import annotations

import json
import logging
from typing import Any

from smi_agent.agents.response import ResponseType, StructuredResponse
from smi_agent.agents.specialists.base import BaseSpecialist
from smi_agent.streaming import StepEmitter

logger = logging.getLogger(__name__)


class RestaurantSpecialist(BaseSpecialist):
    """Finds and ranks restaurants near a location based on rating, price, or cuisine match."""

    @property
    def name(self) -> str:
        return "restaurant"

    @property
    def description(self) -> str:
        return (
            "Search for restaurants near a location, optionally filtered by cuisine. "
            "Returns the best options ranked by rating, price, or cuisine match."
        )

    async def run(
        self,
        query: str,
        context: dict[str, Any],
        step_emitter: StepEmitter,
    ) -> StructuredResponse:
        from smi_agent.examples.travel.tools.restaurant_scraper import search_restaurants

        location = context.get("location", "")
        cuisine = context.get("cuisine")
        sort_by = context.get("sort_by", "rating")

        if not location:
            location, cuisine, sort_by = _parse_query(query, sort_by)

        await step_emitter.emit(
            "restaurant_search",
            "in_progress",
            f"Searching restaurants in {location}"
            + (f" ({cuisine})" if cuisine else "")
            + f" (sort: {sort_by})",
        )

        try:
            restaurants = await search_restaurants(
                location=location,
                cuisine=cuisine,
                sort_by=sort_by,
            )
        except Exception as exc:
            logger.exception("RestaurantSpecialist: search_restaurants failed")
            await step_emitter.emit("restaurant_search", "failed", str(exc))
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.error,
                status="error",
                blocks=[{"type": "error", "message": f"Restaurant search failed: {exc}"}],
            )

        await step_emitter.emit(
            "restaurant_search", "completed", f"Found {len(restaurants)} restaurant(s)"
        )

        if not restaurants:
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.entity_list,
                status="no_data",
                blocks=[{
                    "type": "text",
                    "content": f"No restaurants found in {location}"
                               + (f" matching '{cuisine}'" if cuisine else "")
                               + ". Try broadening the cuisine filter or searching a wider area.",
                }],
            )

        table_rows = [
            {
                "Restaurant": r["name"],
                "Cuisine": r["cuisine"],
                "Rating": f"{r['rating']}/10" if r.get("rating") else "—",
                "Price": r["price_band"],
                "Avg/person": f"£{r['avg_spend_per_person_gbp']}" if r.get("avg_spend_per_person_gbp") else "—",
                "Distance": f"{r['distance_from_location_km']} km" if r.get("distance_from_location_km") else "—",
                "Highlight": r.get("highlight", ""),
            }
            for r in restaurants
        ]

        best = restaurants[0]
        summary = (
            f"Top pick ({sort_by}): {best['name']} — {best['cuisine']}, "
            f"rated {best['rating']}/10, {best['price_band']} "
            f"(~£{best['avg_spend_per_person_gbp']}/person). {best.get('highlight', '')}"
        )

        follow_ups = [
            f"Find restaurants with outdoor seating in {location}",
            "Show me the cheapest options instead",
        ]
        if cuisine:
            follow_ups.append(f"Any Michelin-starred {cuisine} restaurants nearby?")
        else:
            follow_ups.append("Filter to Italian restaurants only")

        return StructuredResponse(
            agent=self.name,
            responseType=ResponseType.entity_list,
            status="success",
            blocks=[
                {"type": "text", "content": summary},
                {"type": "table", "columns": list(table_rows[0].keys()), "rows": table_rows},
            ],
            payload={"restaurants": restaurants, "sort_by": sort_by},
            followUps=follow_ups,
            sources=[{"tool": "search_restaurants", "query": json.dumps(
                {"location": location, "cuisine": cuisine}
            )}],
        )


def _parse_query(query: str, default_sort: str) -> tuple[str, str | None, str]:
    known_cuisines = [
        "italian", "french", "japanese", "indian", "thai", "mexican",
        "british", "mediterranean", "chinese", "greek", "spanish", "american",
    ]
    query_lower = query.lower()

    cuisine = next((c.title() for c in known_cuisines if c in query_lower), None)

    sort_by = default_sort
    if "cheap" in query_lower or "budget" in query_lower:
        sort_by = "price"
    elif "match" in query_lower or cuisine:
        sort_by = "match"

    # Everything that isn't a cuisine word is treated as location (rough heuristic)
    import re
    location = re.sub(r"\b(" + "|".join(known_cuisines) + r")\b", "", query_lower).strip().title()
    location = location or "Unknown"

    return location, cuisine, sort_by
