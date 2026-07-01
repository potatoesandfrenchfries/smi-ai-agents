"""Travel domain entity resolver — page context → agent name routing."""

from smi_agent.domain.interfaces import EntityResolver


class TravelEntityResolver(EntityResolver):
    """Resolves page context types to agent names for the travel domain."""

    def resolve_agent_name(
        self,
        context_type: str | None = None,
        entity_id: str | None = None,
        label: str | None = None,
    ) -> str:
        # List pages → general conversation agent
        if label and context_type:
            return "conversation"

        # Detail pages → specific agent or conversation
        detail_agents = {
            "flight": "conversation",
            "hotel": "conversation",
            "booking": "conversation",
            "destination": "conversation",
            "airline": "conversation",
            "traveler": "conversation",
            "itinerary": "conversation",
        }

        if context_type in detail_agents:
            return detail_agents[context_type]

        return "conversation"

    def entity_id_required(self, context_type: str) -> bool:
        # Detail pages need entityId, list pages (with labels) don't
        return context_type in ("flight", "hotel", "booking", "destination", "airline", "traveler", "itinerary")

    def valid_page_types(self) -> set[str]:
        return {"flight", "hotel", "booking", "destination", "airline", "traveler", "itinerary", "dashboard", "general"}