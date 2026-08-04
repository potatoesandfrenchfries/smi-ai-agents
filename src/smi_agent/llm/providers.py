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

Cost policy: Haiku only
------------------------
This project is pinned to run on Claude Haiku only. Every lane the Anthropic
provider serves (reasoning, middle, triage) resolves to the same Haiku model
ID below, and cross-provider fallbacks are disabled (``_build_lane_fallbacks``
returns empty lists), so a Haiku outage cannot silently escalate to a
different, more expensive model on another provider. Every ``LLMRouter(...)``
call site in this codebase passes only ``lane=`` — never ``provider=`` or
``model_overrides=`` — so this table is the single place that determines
which model actually runs. To reintroduce a bigger model for a given lane,
change the table entry deliberately; don't add a per-agent override instead,
or this guarantee silently stops holding.
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
        # Every lane pinned to Haiku — see "Cost policy: Haiku only" above.
        # Not Opus/Sonnet: this project intentionally never spends above the
        # Haiku tier, regardless of which lane an agent declares.
        "reasoning": "anthropic/claude-haiku-4-5-20251001",
        "middle": "anthropic/claude-haiku-4-5-20251001",
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
# Disabled: cross-provider fallback (Claude -> GPT/Mistral) previously
# escalated to a bigger, non-Haiku model on another provider whenever an
# OPENAI_API_KEY/MISTRAL_API_KEY happened to be present in the environment —
# which would silently break the "Haiku only" cost policy above on nothing
# more than an outage. Every lane gets an empty fallback list instead: a
# failed Haiku call surfaces as an error rather than spending on a different
# model tier.

def _build_lane_fallbacks() -> dict[str, list[str]]:
    """Return empty fallback chains for every lane (see comment above)."""
    return {"reasoning": [], "middle": [], "triage": [], "air_gap": []}


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
