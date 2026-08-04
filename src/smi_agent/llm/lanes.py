"""LLM lane → model mapping with primary and fallback model IDs.

Lane definitions (UC-02 spec §8.4 named these reasoning/middle/triage/air_gap
for four different quality tiers — Opus/Sonnet/Haiku/Llama respectively — but
this project's cost policy (see "Cost policy: Haiku only" in
smi_agent.llm.providers) pins reasoning, middle, and triage to the same Haiku
model regardless of nominal tier: no code path or agent definition may select
Opus or Sonnet by lane name alone):
- reasoning  → Claude Haiku (pinned; nominal tier was Opus)
- middle     → Claude Haiku (pinned; nominal tier was Sonnet)
- triage     → Claude Haiku
- air_gap    → Llama-3.3 70B via vLLM — deliberately NOT pinned to Haiku.
               Anthropic has no on-prem/air-gapped offering (see
               PROVIDER_MODELS["anthropic"]["air_gap"] = None in providers.py),
               so this lane exists specifically for deployments that cannot
               call Anthropic's cloud API at all; forcing it to Haiku would
               defeat its purpose. No agent definition currently declares
               lane: air_gap — it stays available for that future use case.

The default provider for all lanes is Anthropic (except air_gap, see above).
Agent definitions can override the provider via ``llm.provider`` or
individual models via ``llm.model_overrides`` — but every current agent
definition and call site sets neither, only ``lane=``, so this table is what
actually decides which model runs. See ``resolve_model()`` for the full
resolution order.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from smi_agent.config.models import LaneName
from smi_agent.llm.providers import LANE_FALLBACKS, PROVIDER_MODELS

logger = logging.getLogger(__name__)

# ── Lane configuration (backward-compatible primary/fallback structure) ────────


class LaneEntry(TypedDict):
    primary: str
    fallback: list[str]


LANE_CONFIG: dict[str, LaneEntry] = {
    "reasoning": {
        "primary": PROVIDER_MODELS["anthropic"]["reasoning"],  # type: ignore[typeddict-item]
        "fallback": LANE_FALLBACKS["reasoning"],
    },
    "middle": {
        "primary": PROVIDER_MODELS["anthropic"]["middle"],  # type: ignore[typeddict-item]
        "fallback": LANE_FALLBACKS["middle"],
    },
    "triage": {
        "primary": PROVIDER_MODELS["anthropic"]["triage"],  # type: ignore[typeddict-item]
        "fallback": LANE_FALLBACKS["triage"],
    },
    "air_gap": {
        "primary": PROVIDER_MODELS["vllm"]["air_gap"],  # type: ignore[typeddict-item]
        "fallback": LANE_FALLBACKS["air_gap"],
    },
}

# ── Helpers ────────────────────────────────────────────────────────────────────


def model_for_lane(lane: LaneName) -> str:
    """Return the default primary model ID for a given lane (Anthropic/vLLM defaults)."""
    return LANE_CONFIG[lane]["primary"]


def fallback_models_for_lane(lane: LaneName) -> list[str]:
    """Return ordered list of fallback model IDs for a given lane."""
    return list(LANE_CONFIG[lane]["fallback"])


def resolve_model(
    lane: LaneName,
    provider: str | None = None,
    model_overrides: dict[str, str] | None = None,
) -> str:
    """Resolve the concrete LiteLLM model ID for a (lane, provider, overrides) triple.

    Resolution order (highest priority first):

    1. ``model_overrides[lane]`` — explicit per-lane model string; wins over everything.
    2. ``provider`` lane mapping — if the provider supports this lane.
    3. Default lane primary (Anthropic for reasoning/middle/triage, vLLM for air_gap).

    Args:
        lane: Semantic quality tier.
        provider: Provider name (anthropic, openai, mistral, ollama, vllm) or None.
        model_overrides: Per-lane model ID overrides from the agent definition.

    Returns:
        LiteLLM model ID string (e.g. ``"anthropic/claude-sonnet-4-6"``).
    """
    # 1. Explicit override wins unconditionally.
    if model_overrides and lane in model_overrides:
        return model_overrides[lane]

    # 2. Provider-level selection.
    if provider:
        from smi_agent.llm.providers import model_for_provider_lane  # noqa: PLC0415

        provider_model = model_for_provider_lane(provider, lane)
        if provider_model:
            return provider_model
        # Provider does not cover this lane — fall through to default with a warning.
        logger.warning(
            "resolve_model: provider=%r has no model for lane=%r, using default",
            provider,
            lane,
        )

    # 3. Default (Anthropic/vLLM).
    return LANE_CONFIG[lane]["primary"]
