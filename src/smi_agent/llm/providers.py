"""LLM provider registry — maps provider × lane to LiteLLM model IDs.

Supported providers
-------------------
- ``anthropic``   — Claude models via Anthropic API (ANTHROPIC_API_KEY)
- ``openai``      — GPT models via OpenAI API (OPENAI_API_KEY)
- ``mistral``     — Mistral models via Mistral API (MISTRAL_API_KEY)
- ``ollama``      — Local Llama/Mistral models via Ollama (no key, OLLAMA_API_BASE)
- ``vllm``        — Self-hosted models via vLLM OpenAI-compatible API (VLLM_API_BASE)

Usage
-----
The provider registry is consumed by ``LLMRouter`` to resolve the concrete
model ID for a given (provider, lane) pair.  Agent definitions can declare a
``provider`` to switch all lanes, or fine-grained ``model_overrides`` for
individual lanes.  Falls back to Anthropic if neither is set.
"""

from __future__ import annotations

from typing import Literal

# ── Type alias ─────────────────────────────────────────────────────────────────

ProviderName = Literal["anthropic", "openai", "mistral", "ollama", "vllm"]

# ── Provider × lane → LiteLLM model ID ────────────────────────────────────────
# All model IDs follow LiteLLM's ``provider/model`` convention.
# ``None`` means the provider has no suitable model for that lane; the router
# falls back to the default (Anthropic) for that lane only.

PROVIDER_MODELS: dict[str, dict[str, str | None]] = {
    "anthropic": {
        "reasoning": "anthropic/claude-opus-4-8",
        "middle": "anthropic/claude-sonnet-4-6",
        "triage": "anthropic/claude-haiku-4-5-20251001",
        "air_gap": None,  # Anthropic has no air-gap / on-prem offering
    },
    "openai": {
        "reasoning": "openai/gpt-4o",
        "middle": "openai/gpt-4o",
        "triage": "openai/gpt-4o-mini",
        "air_gap": None,  # OpenAI has no self-hosted option
    },
    "mistral": {
        "reasoning": "mistral/mistral-large-latest",
        "middle": "mistral/mistral-small-latest",
        "triage": "mistral/mistral-small-latest",
        "air_gap": None,  # Mistral has no public air-gap option
    },
    "ollama": {
        # Local Ollama instance — models must be pulled before use.
        # Override via model_overrides if different model tags are installed.
        "reasoning": "ollama/llama3.3:70b",
        "middle": "ollama/llama3.1:8b",
        "triage": "ollama/llama3.2:3b",
        "air_gap": "ollama/llama3.3:70b",
    },
    "vllm": {
        # vLLM with OpenAI-compatible API.  Requires VLLM_API_BASE env var.
        # Model names must match what is loaded in the vLLM server.
        "reasoning": "openai/meta-llama/Llama-3.3-70B-Instruct",
        "middle": "openai/meta-llama/Llama-3.1-8B-Instruct",
        "triage": "openai/meta-llama/Llama-3.2-3B-Instruct",
        "air_gap": "openai/meta-llama/Llama-3.3-70B-Instruct",
    },
}

# ── Required env vars per provider ────────────────────────────────────────────
# Used at startup to warn when a provider is configured but its key is absent.

PROVIDER_ENV_VARS: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "ollama": [],  # local; no API key needed
    "vllm": ["VLLM_API_BASE"],  # base URL of the vLLM-compatible server
}

# ── Default fallback order per lane (provider-agnostic) ───────────────────────
# If the primary model fails LiteLLM will try each fallback in order.
# Fallbacks are intentionally cross-provider so a Claude outage routes to GPT.

def _build_lane_fallbacks() -> dict[str, list[str]]:
    """Build fallback chains only for providers with configured API keys."""
    import os

    _openai = [
        ("reasoning", "openai/gpt-4o"), ("middle", "openai/gpt-4o"), ("triage", "openai/gpt-4o-mini"),
    ]
    _mistral = [
        ("reasoning", "mistral/mistral-large-latest"), ("middle", "mistral/mistral-small-latest"),
        ("triage", "mistral/mistral-small-latest"),
    ]

    fb: dict[str, list[str]] = {"reasoning": [], "middle": [], "triage": [], "air_gap": []}
    if os.environ.get("OPENAI_API_KEY"):
        for lane, model in _openai:
            fb[lane].append(model)
    if os.environ.get("MISTRAL_API_KEY"):
        for lane, model in _mistral:
            fb[lane].append(model)
    return fb


LANE_FALLBACKS: dict[str, list[str]] = _build_lane_fallbacks()

# ── Helpers ────────────────────────────────────────────────────────────────────


def model_for_provider_lane(provider: str, lane: str) -> str | None:
    """Return the LiteLLM model ID for a provider/lane pair, or None if unsupported."""
    return PROVIDER_MODELS.get(provider, {}).get(lane)


def validate_provider_env(provider: str) -> list[str]:
    """Return list of missing env var names for the given provider.

    Returns an empty list when all required vars are present.
    """
    import os  # noqa: PLC0415

    required = PROVIDER_ENV_VARS.get(provider, [])
    return [v for v in required if not os.environ.get(v)]
