"""FlightSpecialist — searches for flights and returns ranked options.

Implements BaseSpecialist directly so the agent's run() method calls the
flight provider in Python rather than routing through the ToolRegistry. This
makes the data flow explicit and easy to follow for learning purposes. The
provider registry (not a concrete scraper) is the only thing this agent
depends on, so a new flight data source needs no change here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from smi_agent.agents.response import ResponseType, StructuredResponse
from smi_agent.agents.specialists.base import BaseSpecialist
from smi_agent.streaming import StepEmitter

logger = logging.getLogger(__name__)


class FlightSpecialist(BaseSpecialist):
    """Finds and ranks available flights for a given route and date."""

    @property
    def name(self) -> str:
        return "flight"

    @property
    def description(self) -> str:
        return (
            "Search for available flights between two airports on a specific date. "
            "Returns the best options ranked by cost, comfort, or journey time. "
            "Provide origin, destination, date, and optionally a sort preference."
        )

    async def run(
        self,
        query: str,
        context: dict[str, Any],
        step_emitter: StepEmitter,
    ) -> StructuredResponse:
        from smi_agent.providers.registry import get_flight_provider

        # ── 1. Parse parameters from context (set by the Planner or API layer) ──
        origin = context.get("origin", "")
        destination = context.get("destination", "")
        date = context.get("date", "")
        sort_by = context.get("sort_by", "cost")

        # Fall back to extracting from the raw query string if context is sparse
        if not (origin and destination and date):
            logger.warning("FlightSpecialist: incomplete context %s — parsing query", context)
            origin, destination, date, sort_by = _parse_query(query, sort_by)

        await step_emitter.emit(
            "flight_search",
            "in_progress",
            f"Searching flights from {origin} to {destination} on {date} (sort: {sort_by})",
        )

        # ── 2. Call the scraper tool ──────────────────────────────────────────
        try:
            flights = await get_flight_provider().search(
                origin=origin,
                destination=destination,
                date=date,
                sort_by=sort_by,
            )
        except Exception as exc:
            logger.exception("FlightSpecialist: flight provider search failed")
            await step_emitter.emit("flight_search", "failed", str(exc))
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.error,
                status="error",
                blocks=[{"type": "error", "message": f"Flight search failed: {exc}"}],
            )

        await step_emitter.emit(
            "flight_search",
            "completed",
            f"Found {len(flights)} flight(s)",
        )

        # ── 3. Build structured response ──────────────────────────────────────
        if not flights:
            return StructuredResponse(
                agent=self.name,
                responseType=ResponseType.entity_list,
                status="no_data",
                blocks=[{
                    "type": "text",
                    "content": f"No flights found from {origin} to {destination} on {date}. "
                               "Try adjusting the date or nearby airports.",
                }],
            )

        table_rows = [
            {
                "Airline": f["airline"],
                "Departure": f["departure"][11:16] if f["departure"] else "—",
                "Arrival": f["arrival"][11:16] if f["arrival"] else "—",
                "Duration": _fmt_duration(f.get("duration_min")),
                "Stops": str(f.get("stops", 0)),
                "Price (GBP)": f"£{f['price_gbp']:.2f}" if f.get("price_gbp") else "—",
                "Seats left": str(f.get("seats_remaining") or "—"),
            }
            for f in flights
        ]

        best = flights[0]
        stops_str = "direct" if best["stops"] == 0 else f"{best['stops']} stop(s)"
        price_str = f"£{best['price_gbp']:.2f}" if best.get("price_gbp") else "price unavailable"
        summary = (
            f"Best option ({sort_by}): {best['airline']} departing {best['departure'][11:16]} "
            f"— {_fmt_duration(best.get('duration_min'))}, {stops_str}, {price_str}"
        )

        return StructuredResponse(
            agent=self.name,
            responseType=ResponseType.entity_list,
            status="success",
            blocks=[
                {"type": "text", "content": summary},
                {"type": "table", "columns": list(table_rows[0].keys()), "rows": table_rows},
            ],
            payload={"flights": flights, "sort_by": sort_by},
            followUps=[
                f"Show me the cheapest option in detail",
                f"Are there flights on the day before or after?",
                f"Filter to direct flights only",
            ],
            sources=[{"tool": "search_flights", "query": json.dumps({"origin": origin, "destination": destination, "date": date})}],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m"


def _parse_query(query: str, default_sort: str) -> tuple[str, str, str, str]:
    """Very basic keyword extraction for prototype purposes.

    In production this would be handled by the LLM parsing the query before
    calling the specialist.
    """
    words = query.upper().split()
    # Look for 3-letter IATA-like codes
    codes = [w for w in words if len(w) == 3 and w.isalpha()]
    origin = codes[0] if len(codes) > 0 else "UNK"
    destination = codes[1] if len(codes) > 1 else "UNK"

    # Look for a date pattern YYYY-MM-DD
    import re
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", query)
    date = dates[0] if dates else "2026-08-01"

    sort_by = default_sort
    if "comfort" in query.lower():
        sort_by = "comfort"
    elif "time" in query.lower() or "fastest" in query.lower() or "quickest" in query.lower():
        sort_by = "time"
    elif "cheap" in query.lower() or "price" in query.lower() or "cost" in query.lower():
        sort_by = "cost"

    return origin, destination, date, sort_by
