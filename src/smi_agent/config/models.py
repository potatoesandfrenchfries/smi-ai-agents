"""Pydantic v2 models for the AgentDefinition YAML config.

Every field is optional except ``name``, ``version``, and
``output_schema_variant``; missing fields get sensible defaults so slim
configs work out of the box.

The ``hash`` field is a runtime-only value set by the loader in one shot via
``model_validate``; it never appears in YAML (the loader strips it before
validation). It is excluded from serialization (``model_dump``/JSON) but
preserved correctly by ``model_copy()``, preventing blank-hash clones in
downstream LangGraph state passing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smi_agent.config.defaults import DEFAULT_NODE_LABELS

logger = logging.getLogger(__name__)

# ── Lane type ──────────────────────────────────────────────────────────────────

LaneName = Literal["reasoning", "middle", "triage", "air_gap"]

# ── Provider type ──────────────────────────────────────────────────────────────

ProviderName = Literal["anthropic", "openai", "mistral", "ollama", "vllm"]

# ── Sub-models ─────────────────────────────────────────────────────────────────


class LLMConfig(BaseModel):
    lane: LaneName = "middle"
    temperature: Annotated[float, Field(ge=0.0, le=1.0)] = 0.2
    max_tokens_per_call: Annotated[int, Field(gt=0)] = 4000
    prompt_templates_dir: str = "prompts/default"

    # ── Provider configuration ─────────────────────────────────────────────────
    # When set, all lanes use models from this provider (unless overridden below).
    # Omit to keep the default (Anthropic for reasoning/middle/triage, vLLM for air_gap).
    provider: ProviderName | None = None

    # Per-lane model overrides: {"middle": "openai/gpt-4o", "triage": "mistral/mistral-small-latest"}
    # Takes precedence over both ``provider`` and the lane defaults.
    # Values must be valid LiteLLM model ID strings (``provider/model-name`` format).
    model_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("prompt_templates_dir")
    @classmethod
    def _no_path_traversal(cls, v: str) -> str:
        """Reject absolute paths and ``..`` components.

        ``prompt_templates_dir`` will be used to open a directory in M4;
        validate the shape now before the consumer exists.
        """
        p = Path(v)
        if p.is_absolute():
            raise ValueError(f"prompt_templates_dir must be a relative path, got: {v!r}")
        if ".." in p.parts:
            raise ValueError(f"prompt_templates_dir must not contain '..' components, got: {v!r}")
        return v

    @field_validator("model_overrides")
    @classmethod
    def _validate_model_overrides(cls, v: dict[str, str]) -> dict[str, str]:
        """Reject override keys that are not valid lane names."""
        valid_lanes = {"reasoning", "middle", "triage", "air_gap"}
        unknown = set(v.keys()) - valid_lanes
        if unknown:
            raise ValueError(
                f"model_overrides contains unknown lane(s): {sorted(unknown)}. "
                f"Valid lanes: {sorted(valid_lanes)}"
            )
        return v


class GraphPolicy(BaseModel):
    max_depth: Annotated[int, Field(ge=1)] = 4
    hop_budget: Annotated[int, Field(ge=1)] = 50
    max_queries: Annotated[int, Field(ge=1)] = 25
    allowed_node_labels: list[str] = Field(default_factory=lambda: list(DEFAULT_NODE_LABELS))
    allowed_cypher_templates: list[str] = Field(default_factory=list)
    allow_raw_cypher: bool = False

    @model_validator(mode="after")
    def _warn_if_no_queries_possible(self) -> GraphPolicy:
        """Log a warning when no graph queries can execute.

        Uses ``logger.warning`` rather than ``warnings.warn`` so the message
        appears consistently in structured logs and is not filtered by Python's
        default warning filter in worker processes.
        """
        if not self.allowed_cypher_templates and not self.allow_raw_cypher:
            logger.warning(
                "GraphPolicy: allowed_cypher_templates is empty and allow_raw_cypher "
                "is False — no graph queries will execute. Add template names to "
                "allowed_cypher_templates or set allow_raw_cypher: true (with caution)."
            )
        return self


class PlanConfig(BaseModel):
    num_alternatives: Annotated[int, Field(ge=0, le=3)] = 2
    required_alternative_types: list[str] = Field(
        default_factory=lambda: ["canonical_fix", "compensating_control"]
    )
    optional_alternative_types: list[str] = Field(default_factory=list)
    min_severity_to_run: Literal["low", "medium", "high", "critical"] = "medium"
    require_rollback: bool = True
    require_impact_analysis: bool = True
    require_relationship_analysis: bool = True


class BudgetConfig(BaseModel):
    max_cost_usd: Annotated[float, Field(gt=0)] = 0.75
    max_tokens_total: Annotated[int, Field(gt=0)] = 50_000
    max_tokens_input: Annotated[int, Field(gt=0)] = 40_000
    max_tokens_output: Annotated[int, Field(gt=0)] = 10_000
    max_llm_calls: Annotated[int, Field(gt=0)] = 20
    max_tokens_per_call_input: Annotated[int, Field(gt=0)] = 12_000
    max_tokens_per_call_output: Annotated[int, Field(gt=0)] = 4_000
    per_node_max_cost_usd: dict[str, float] = Field(default_factory=dict)
    on_budget_exceeded: Literal["fail_fast", "return_partial", "downgrade_lane"] = "fail_fast"
    warning_threshold_pct: Annotated[float, Field(ge=0, le=100)] = 80.0
    hard_stop_threshold_pct: Annotated[float, Field(ge=0, le=100)] = 100.0
    downgrade_to_lane: LaneName | None = None

    @model_validator(mode="after")
    def _require_downgrade_target(self) -> BudgetConfig:
        if self.on_budget_exceeded == "downgrade_lane" and self.downgrade_to_lane is None:
            raise ValueError(
                "on_budget_exceeded='downgrade_lane' requires downgrade_to_lane to be set"
            )
        return self

    @model_validator(mode="after")
    def _token_budgets_coherent(self) -> BudgetConfig:
        """Catch misconfigured token budgets at load time, not mid-investigation."""
        if self.max_tokens_input + self.max_tokens_output > self.max_tokens_total:
            raise ValueError(
                f"max_tokens_input ({self.max_tokens_input}) + "
                f"max_tokens_output ({self.max_tokens_output}) "
                f"exceeds max_tokens_total ({self.max_tokens_total})"
            )
        if self.max_tokens_per_call_input > self.max_tokens_input:
            raise ValueError(
                f"max_tokens_per_call_input ({self.max_tokens_per_call_input}) "
                f"exceeds max_tokens_input ({self.max_tokens_input})"
            )
        return self


class GuardrailsConfig(BaseModel):
    # ge=1: zero means "no timeout" which is never intentional in production.
    node_timeout_seconds: Annotated[int, Field(ge=1)] = 60
    total_timeout_seconds: Annotated[int, Field(ge=1)] = 300
    heartbeat_interval_seconds: Annotated[int, Field(ge=1)] = 10


class ObservabilityConfig(BaseModel):
    langfuse_session_tag: str = "default"
    trace_cypher_queries: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class ConversationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_history_turns: int = 20
    compaction_threshold_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    summary_lane: str = "triage"
    session_ttl_seconds: int = 604800
    page_context_types: list[str] = Field(
        default_factory=lambda: ["entity", "list", "dashboard", "general"]
    )

    # Ceiling limits (per conversation)
    ceiling_max_messages: int = Field(default=100, ge=1)
    ceiling_max_tokens: int = Field(default=200_000, ge=1000)
    ceiling_warning_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    ceiling_critical_pct: float = Field(default=90.0, ge=0.0, le=100.0)
    max_active_conversations_per_user: int = Field(default=10, ge=1)

    # Persistence
    persist_to_postgres: bool = True


class ToolCallingConfig(BaseModel):
    """ReAct tool-calling loop configuration for conversation agents."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    max_tool_iterations: int = Field(default=5, ge=1, le=20)
    tool_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    enabled_tools: list[str] = Field(default_factory=list)  # empty = all allowed
    max_llm_calls: int = Field(default=8, ge=1, le=20)
    max_cost_usd: float = Field(default=0.25, gt=0.0, le=2.0)


class PostgresConfig(BaseModel):
    """Canned Postgres query allowlist for the agent."""

    allowed_queries: list[str] = Field(
        default_factory=lambda: [
            # Generic conversation queries
            "insert_conversation",
            "fetch_conversation",
            "list_conversations",
            "count_conversations",
            "count_active_conversations",
            "insert_conversation_message",
            "fetch_conversation_messages",
            "count_conversation_messages",
            "fetch_conversation_recent_messages",
            "update_conversation_atomic",
            "next_message_seq",
            "update_conversation_status",
            "update_conversation_title",
            "fetch_last_message_preview",
            "update_conversation_template",
            "fetch_conversation_template",
            # Investigation
            "insert_investigation",
            "insert_audit_log",
            "update_investigation_state",
        ]
    )


# ── Root model ─────────────────────────────────────────────────────────────────


class AgentDefinition(BaseModel):
    """Root config model loaded from ``agent_definitions/<name>.yaml``.

    Required fields: ``name``, ``version``, ``output_schema_variant``.
    All other fields have defaults and may be omitted in the YAML.

    ``hash`` is a runtime value injected by the loader via ``model_validate``;
    it is excluded from ``model_dump()`` so it does not round-trip into YAML,
    but it *is* preserved by ``model_copy()`` — preventing blank-hash clones
    when the config is passed through LangGraph state.

    ``graph_topology`` is deferred to M6 when ``build_agent_graph()`` consumes it.
    """

    model_config = {
        # Pydantic silently ignores extra keys; loader.py warns on them.
        "extra": "ignore",
        "populate_by_name": True,
    }

    # ── Required ──────────────────────────────────────────────────────────────
    name: str
    version: str
    output_schema_variant: Literal["full_v1", "minimal_v1", "ticket_only_v1"]

    # ── Optional with defaults ────────────────────────────────────────────────
    description: str = ""
    runtime_profile: Literal["conversation", "workflow", "coordinator"] = "workflow"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    graph: GraphPolicy = Field(default_factory=GraphPolicy)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    # graph_topology: NodeSpec/EdgeSpec/GraphTopology added in M6.
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    tool_calling: ToolCallingConfig = Field(default_factory=ToolCallingConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    context_window_tokens: int = 200_000

    # ── Runtime-only ──────────────────────────────────────────────────────────
    # Set by loader in one shot via model_validate({**data, "hash": ...}).
    # Excluded from model_dump() so it does not appear in serialised output,
    # but preserved by model_copy() unlike fields set by post-construction mutation.
    hash: str = Field(default="", exclude=True, repr=False)
