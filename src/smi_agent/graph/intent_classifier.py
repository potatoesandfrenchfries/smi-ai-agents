"""IntentClassifierAgent — routes a traveler's raw message to the right action.

Every message used to be treated as "generate a full itinerary". This
distinguishes that from lighter-weight asks (a one-off flight/hotel/
restaurant search, a budget question, an edit to an existing plan) so the
caller (scripts/nlcli.py) can route to the cheapest codepath that actually
answers the question instead of always running the full search+compile+HITL
pipeline.

Structured the same way as the specialist agents in agents/specialists/*.py
(name/description/run()) for consistency, but deliberately independent of
BaseSpecialist and StructuredResponse — those belong to the dormant
Supervisor/chat-frontend stack, and this classifier is part of the active
Temporal + LangGraph pipeline, returning plain IntentResult data instead of
a ContentBlock-based response envelope.

Same two-tier convention as parse_intent's _llm_parse in itinerary_graph.py:
a cheap triage-lane LLM call returning JSON, with a keyword-based fallback
if that call fails for any reason.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from smi_agent.observability.logging import get_planner_trace_logger

logger = logging.getLogger(__name__)
trace_logger = get_planner_trace_logger()


class Intent(StrEnum):
    GENERATE_ITINERARY = "generate_itinerary"
    MODIFY_ITINERARY = "modify_itinerary"
    FLIGHT_SEARCH = "flight_search"
    HOTEL_SEARCH = "hotel_search"
    RESTAURANT_SEARCH = "restaurant_search"
    BUDGET_QUERY = "budget_query"


@dataclass
class IntentResult:
    intent: Intent
    confidence: float
    reasoning: str


_MIN_CONFIDENCE = 0.5
_PROMPT_DIR = "prompts/agents/intent_classifier"


def _keyword_classify(raw_goal: str) -> IntentResult:
    text = raw_goal.lower()

    modify_words = ("change", "modify", "instead", "swap", "switch", "different")
    if any(w in text for w in modify_words):
        return IntentResult(
            Intent.MODIFY_ITINERARY, 0.6, "Keyword fallback matched a change/swap phrase"
        )

    budget_words = ("budget", "afford", "how much", "cost", "expensive", "cheap")
    plan_verbs = ("plan", "book", "itinerary", "trip to", "fly from", "flying from")
    has_budget_word = any(w in text for w in budget_words)
    has_plan_verb = any(w in text for w in plan_verbs)
    if has_budget_word and not has_plan_verb:
        return IntentResult(
            Intent.BUDGET_QUERY, 0.6, "Keyword fallback matched a cost/budget phrase without a planning verb"
        )

    domain_words = {
        Intent.FLIGHT_SEARCH: ("flight", "flights", "fly "),
        Intent.HOTEL_SEARCH: ("hotel", "hotels", "stay", "accommodation"),
        Intent.RESTAURANT_SEARCH: ("restaurant", "restaurants", "dining", "eat", "food"),
    }
    matched = [intent for intent, words in domain_words.items() if any(w in text for w in words)]
    if len(matched) == 1 and not has_plan_verb:
        return IntentResult(
            matched[0], 0.55, f"Keyword fallback matched only {matched[0].value} vocabulary"
        )

    return IntentResult(Intent.GENERATE_ITINERARY, 0.5, "Keyword fallback default")


class IntentClassifierAgent:
    """Classifies a traveler's raw message into an Intent."""

    @property
    def name(self) -> str:
        return "intent_classifier"

    @property
    def description(self) -> str:
        return "Classifies a traveler's message into an action intent and routes accordingly."

    async def run(self, raw_goal: str) -> IntentResult:
        """Classify raw_goal, falling back to GENERATE_ITINERARY — the safest
        default, since it's the only path that always makes sense for a
        first-time message — whenever the LLM call fails or returns a
        low-confidence result.
        """
        result = await self._llm_classify(raw_goal)
        if result is None:
            fallback = _keyword_classify(raw_goal)
            trace_logger.info(
                "INTENT goal=%r -> intent=%s confidence=%.2f (keyword fallback: %s)",
                raw_goal[:160], fallback.intent.value, fallback.confidence, fallback.reasoning,
            )
            return fallback
        if result.confidence < _MIN_CONFIDENCE:
            logger.info(
                "[%s] low confidence (%.2f) for intent=%s — defaulting to generate_itinerary",
                self.name, result.confidence, result.intent.value,
            )
            trace_logger.info(
                "INTENT goal=%r -> intent=%s confidence=%.2f -> defaulted to generate_itinerary (low confidence)",
                raw_goal[:160], result.intent.value, result.confidence,
            )
            return IntentResult(
                Intent.GENERATE_ITINERARY, result.confidence,
                f"Low-confidence classification ({result.intent.value}) defaulted to generate_itinerary",
            )
        trace_logger.info(
            "INTENT goal=%r -> intent=%s confidence=%.2f reasoning=%s",
            raw_goal[:160], result.intent.value, result.confidence, result.reasoning,
        )
        return result

    async def _llm_classify(self, raw_goal: str) -> IntentResult | None:
        try:
            from smi_agent.llm.prompts import PromptLoader
            from smi_agent.llm.router import LLMRouter

            prompt_loader = PromptLoader(_PROMPT_DIR)
            system_block = prompt_loader.render_system("system", {})

            router = LLMRouter(lane="triage", temperature=0.0)
            result = await router.call(
                messages=[
                    {"role": "system", "content": [system_block]},
                    {"role": "user", "content": raw_goal},
                ],
                trace_name="classify_intent",
            )
            text = result.content.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text.strip())
            parsed = json.loads(text)

            intent = Intent(parsed["intent"])
            confidence = float(parsed.get("confidence", 0.0))
            reasoning = str(parsed.get("reasoning", ""))
            return IntentResult(intent=intent, confidence=confidence, reasoning=reasoning)
        except Exception as exc:
            logger.warning("[%s] LLM classification failed (%s) — falling back to keywords", self.name, exc)
            return None
