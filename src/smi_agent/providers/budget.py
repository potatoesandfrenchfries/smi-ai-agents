"""Budget providers — concrete implementations of BudgetProvider.

Both classes implement the same cheaper-alternative-combo logic that used
to live inline in graph/itinerary_graph.py::budget_agent — moved here so it
is independently callable (e.g. as an MCP tool) instead of only reachable
from inside that one graph node. There is no "mock vs real" data-source
split like flight/hotel/restaurant have, since this never calls an external
API: it only re-ranks candidates the caller already fetched. Both classes
exist to match the registry's env-var-selectable pattern; MockBudgetProvider
is a plain alias kept for interface symmetry with the other providers.
"""

from __future__ import annotations

from typing import Any


def _combo_cost(
    flight: dict[str, Any], hotel: dict[str, Any],
    restaurants: list[dict[str, Any]], attractions: list[dict[str, Any]],
) -> float:
    return round(
        (flight.get("price_gbp") or 0)
        + (hotel.get("total_price_gbp") or 0)
        + sum(r.get("avg_spend_per_person_gbp") or 0 for r in restaurants)
        + sum(a.get("entry_fee_gbp") or 0 for a in attractions),
        2,
    )


class DefaultBudgetProvider:
    """Suggests cheaper alternative flight/hotel/dining combos.

    Re-ranks candidates already gathered elsewhere — no new searches are
    issued, matching FR-ORC-3 (no full re-fetch just to compare cost).
    """

    async def search(
        self,
        flights: list[dict[str, Any]],
        hotels: list[dict[str, Any]],
        restaurants: list[dict[str, Any]],
        attractions: list[dict[str, Any]],
        current_total_gbp: float,
        budget_gbp: float | None,
        trip_type: str = "leisure",
        num_results: int = 3,
    ) -> list[dict[str, Any]]:
        current_flight = flights[0] if flights else {}
        current_hotel = hotels[0] if hotels else {}
        current_restaurants = restaurants[:3]
        current_attractions = attractions[:3] if trip_type == "leisure" else []

        cheapest_flight = min(flights, key=lambda f: f.get("price_gbp") or float("inf"), default={})
        cheapest_hotel = min(hotels, key=lambda h: h.get("total_price_gbp") or float("inf"), default={})
        cheapest_restaurants = sorted(
            restaurants, key=lambda r: r.get("avg_spend_per_person_gbp") or float("inf")
        )[:3]

        candidates: list[dict[str, Any]] = []

        if cheapest_hotel and cheapest_hotel.get("id") != current_hotel.get("id"):
            total = _combo_cost(current_flight, cheapest_hotel, current_restaurants, current_attractions)
            candidates.append({
                "label": f"Switch hotel to {cheapest_hotel.get('name', 'a cheaper option')}",
                "total_cost_gbp": total,
            })

        if cheapest_flight and cheapest_flight.get("id") != current_flight.get("id"):
            total = _combo_cost(cheapest_flight, current_hotel, current_restaurants, current_attractions)
            candidates.append({
                "label": f"Switch flight to {cheapest_flight.get('airline', 'a cheaper option')} "
                         f"({cheapest_flight.get('departure', 'alternate time')})",
                "total_cost_gbp": total,
            })

        combo_label = "Switch to the cheapest flight, hotel, and dining combo"
        if trip_type == "leisure" and attractions:
            combo_label += " and skip the optional attractions"
        candidates.append({
            "label": combo_label,
            "total_cost_gbp": _combo_cost(cheapest_flight, cheapest_hotel, cheapest_restaurants, []),
        })

        seen_totals: set[float] = set()
        alternatives: list[dict[str, Any]] = []
        for c in sorted(candidates, key=lambda c: c["total_cost_gbp"]):
            savings = round(current_total_gbp - c["total_cost_gbp"], 2)
            if savings <= 0 or c["total_cost_gbp"] in seen_totals:
                continue
            seen_totals.add(c["total_cost_gbp"])
            alternatives.append({
                "label": c["label"],
                "total_cost_gbp": c["total_cost_gbp"],
                "savings_gbp": savings,
                "within_budget": bool(budget_gbp) and c["total_cost_gbp"] <= budget_gbp,
            })
            if len(alternatives) == num_results:
                break

        return alternatives


class MockBudgetProvider(DefaultBudgetProvider):
    """Alias of DefaultBudgetProvider — no external calls to mock in the first place."""
