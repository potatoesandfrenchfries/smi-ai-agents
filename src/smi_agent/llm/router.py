"""LiteLLM wrapper with lane routing, budget enforcement, and Langfuse callback.

The ``LLMRouter`` is the *only* path through which the agent makes LLM calls.

Usage inside a node::

    router = LLMRouter(lane="middle", langfuse_session_id=investigation_id)
    result = await router.call(
        messages=[{"role": "user", "content": "..."}],
        budget=budget_tracker,
    )
    budget = budget.record_call(result.tokens_input, result.tokens_output, result.cost_usd)

Provider selection
------------------
Model resolution order (highest priority first):

1. ``model_overrides[lane]`` on the ``LLMRouter`` instance — explicit per-lane
   model string.
2. ``provider`` on the ``LLMRouter`` instance — selects the provider's canonical
   model for the lane.
3. Lane default (Anthropic for reasoning/middle/triage, vLLM for air_gap).

``call()`` raises ``BudgetExceededError`` *before* the LLM call if the
pre-flight budget check fails.  It propagates ``litellm.exceptions.*`` on
API errors so the node can handle them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import litellm

if TYPE_CHECKING:
    from litellm import CustomStreamWrapper

from smi_agent.config.models import LaneName
from smi_agent.llm.lanes import LANE_CONFIG, resolve_model
from smi_agent.llm.providers import validate_provider_env
from smi_agent.schemas.internal import BudgetTracker

logger = logging.getLogger(__name__)

# Silence litellm's verbose INFO logs unless explicitly requested.
litellm.suppress_debug_info = True


# ── Result type ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMCallResult:
    """Structured return value from ``LLMRouter.call()``."""

    content: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    model: str
    lane: LaneName
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Router ─────────────────────────────────────────────────────────────────────


class LLMRouter:
    """Makes async LLM calls via LiteLLM with lane routing and budget enforcement.

    Args:
        lane: Default routing lane for calls made through this router instance.
        provider: LLM provider (anthropic, openai, mistral, ollama, vllm).
            When set, overrides the per-lane default provider.  Individual
            lanes can still be overridden via ``model_overrides``.
        model_overrides: Per-lane model ID overrides.  Keys are lane names;
            values are LiteLLM model ID strings (e.g. ``"openai/gpt-4o"``).
            Takes precedence over both ``provider`` and lane defaults.
        langfuse_session_id: If set and ``LANGFUSE_SECRET_KEY`` is in env,
            traces are sent to Langfuse under this session ID.
        temperature: Override; falls back to lane default.
        investigation_id: Injected into Langfuse trace metadata.
    """

    def __init__(
        self,
        lane: LaneName = "middle",
        provider: str | None = None,
        model_overrides: dict[str, str] | None = None,
        langfuse_session_id: str | None = None,
        temperature: float = 0.2,
        investigation_id: str | None = None,
    ) -> None:
        self._default_lane = lane
        self._provider = provider
        self._model_overrides = model_overrides or {}
        self._langfuse_session_id = langfuse_session_id
        self._temperature = temperature
        self._investigation_id = investigation_id
        self._langfuse_enabled = bool(os.environ.get("LANGFUSE_SECRET_KEY"))

        # Warn at construction time if provider env vars are missing.
        if provider:
            missing = validate_provider_env(provider)
            if missing:
                logger.warning(
                    "LLMRouter: provider=%r is configured but env var(s) %s are not set — "
                    "calls will likely fail",
                    provider,
                    missing,
                )

        # Register Langfuse callbacks once at construction, not per-call.
        if self._langfuse_enabled:
            if "langfuse" not in litellm.success_callback:
                litellm.success_callback.append("langfuse")
            if "langfuse" not in litellm.failure_callback:
                litellm.failure_callback.append("langfuse")

    async def call(
        self,
        messages: list[dict[str, Any]],
        budget: BudgetTracker | None = None,
        lane_override: LaneName | None = None,
        estimated_tokens_in: int = 0,
        estimated_tokens_out: int = 0,
        estimated_cost: float = 0.0,
        trace_name: str | None = None,
        **litellm_kwargs: Any,
    ) -> LLMCallResult:
        """Execute an LLM call through the configured lane.

        Args:
            messages: OpenAI-format message list.
            budget: Current ``BudgetTracker``; pre-flight check run if provided.
            lane_override: Use a different lane for this call only.
            estimated_tokens_in: For budget pre-flight (use 0 if unknown).
            estimated_tokens_out: For budget pre-flight.
            estimated_cost: For budget pre-flight.
            trace_name: Langfuse trace/generation name.
            **litellm_kwargs: Forwarded to ``litellm.acompletion``.

        Returns:
            ``LLMCallResult`` with actual token counts and cost.

        Raises:
            BudgetExceededError: Pre-flight budget check failed.
            litellm.exceptions.*: API-level failure.
        """
        lane: LaneName = lane_override or self._default_lane

        # Resolve model: overrides → provider → lane default.
        model = resolve_model(
            lane=lane,
            provider=self._provider,
            model_overrides=self._model_overrides,
        )
        fallbacks = list(LANE_CONFIG[lane]["fallback"])

        # ── Pre-flight budget check ────────────────────────────────────────────
        if budget is not None:
            budget.check_can_call(
                tokens_in=estimated_tokens_in,
                tokens_out=estimated_tokens_out,
                estimated_cost=estimated_cost,
            )

        # ── Build LiteLLM kwargs ───────────────────────────────────────────────
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            **litellm_kwargs,
        }

        if fallbacks:
            call_kwargs["fallbacks"] = [{"model": m} for m in fallbacks]

        # ── Provider-specific base URL (Ollama / vLLM) ────────────────────────
        if self._provider == "ollama":
            base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
            call_kwargs.setdefault("api_base", base_url)
        elif self._provider == "vllm":
            base_url = os.environ.get("VLLM_API_BASE", "")
            if base_url:
                call_kwargs.setdefault("api_base", base_url)

        # ── Langfuse metadata ──────────────────────────────────────────────────
        if self._langfuse_enabled:
            metadata: dict[str, Any] = {}
            if self._langfuse_session_id:
                metadata["session_id"] = self._langfuse_session_id
            if self._investigation_id:
                metadata["trace_id"] = self._investigation_id
            if trace_name:
                metadata["generation_name"] = trace_name
            if metadata:
                call_kwargs["metadata"] = metadata

        logger.debug(
            "LLM call lane=%s model=%s provider=%s session=%s",
            lane,
            model,
            self._provider or "default",
            self._langfuse_session_id,
        )

        # ── Execute ────────────────────────────────────────────────────────────
        response = await litellm.acompletion(**call_kwargs)

        message = response.choices[0].message
        content: str = message.content or ""
        usage = response.usage or {}

        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0

        # Extract tool calls (for ReAct agent loop)
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [
            {
                "id": tc.id,
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in raw_tool_calls
        ]
        finish_reason = getattr(response.choices[0], "finish_reason", "") or ""

        # litellm.completion_cost returns cost in USD for the response
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception as cost_exc:
            logger.warning(
                "LLM cost calculation failed (%s) — cost recorded as $0.00; "
                "budget cap may be unreliable for this call",
                type(cost_exc).__name__,
            )
            cost = 0.0

        return LLMCallResult(
            content=content,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost,
            model=model,
            lane=lane,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def stream_call(
        self,
        messages: list[dict[str, Any]],
        budget: BudgetTracker | None = None,
        lane_override: LaneName | None = None,
        estimated_tokens_in: int = 0,
        estimated_tokens_out: int = 0,
        estimated_cost: float = 0.0,
        trace_name: str | None = None,
        **litellm_kwargs: Any,
    ) -> tuple[CustomStreamWrapper, LaneName, str]:
        """Start a streaming LLM call. Returns (stream, lane, model).

        The caller iterates the stream to get delta chunks, then calls
        ``stream.get_final_usage()`` after iteration to get token counts.

        Usage::

            stream, lane, model = await router.stream_call(messages=...)
            full_text = ""
            async for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                full_text += delta
                yield delta  # push to SSE
            # After iteration, get usage
            usage = stream.usage  # may be None for some providers
        """
        lane: LaneName = lane_override or self._default_lane
        model = resolve_model(
            lane=lane,
            provider=self._provider,
            model_overrides=self._model_overrides,
        )
        fallbacks = list(LANE_CONFIG[lane]["fallback"])

        if budget is not None:
            budget.check_can_call(
                tokens_in=estimated_tokens_in,
                tokens_out=estimated_tokens_out,
                estimated_cost=estimated_cost,
            )

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **litellm_kwargs,
        }

        if fallbacks:
            call_kwargs["fallbacks"] = [{"model": m} for m in fallbacks]

        if self._provider == "ollama":
            base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
            call_kwargs.setdefault("api_base", base_url)
        elif self._provider == "vllm":
            base_url = os.environ.get("VLLM_API_BASE", "")
            if base_url:
                call_kwargs.setdefault("api_base", base_url)

        if self._langfuse_enabled:
            metadata: dict[str, Any] = {}
            if self._langfuse_session_id:
                metadata["session_id"] = self._langfuse_session_id
            if self._investigation_id:
                metadata["trace_id"] = self._investigation_id
            if trace_name:
                metadata["generation_name"] = trace_name
            if metadata:
                call_kwargs["metadata"] = metadata

        logger.debug(
            "LLM stream call lane=%s model=%s provider=%s",
            lane, model, self._provider or "default",
        )

        response = await litellm.acompletion(**call_kwargs)
        return response, lane, model
