"""Internal per-node types: BudgetTracker, ScopeResult, GraphContext, AgentState.

These are *not* part of the public activity contract — they live only within the
LangGraph graph execution and are never serialized across the Temporal boundary.

``AgentState`` is a ``TypedDict`` (not ``BaseModel``) for LangGraph compatibility:
``StateGraph`` requires a ``TypedDict`` as its state schema argument.  Pydantic models
inside the state (e.g. ``BudgetTracker``) are reconstructed from dicts at each node
entry by helper utilities (implemented in M5).

``BudgetTracker`` is frozen (immutable).  Nodes must call ``record_call()`` and use
the returned new instance — never mutate in place.  This is safe for LangGraph state
snapshots and the Postgres checkpointer.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from smi_agent.config.models import BudgetConfig, LaneName
from smi_agent.schemas.plan import PlanEnvelope, RunMetrics

# ── Budget exceeded error ──────────────────────────────────────────────────────


class BudgetExceededError(Exception):
    """Raised by ``BudgetTracker.check_can_call()`` when a budget cap would be breached.

    Carries structured fields so callers can pattern-match the dimension without
    string parsing.
    """

    def __init__(
        self,
        dimension: str,
        current: float | int,
        limit: float | int,
        node_name: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.current = current
        self.limit = limit
        self.node_name = node_name
        node_part = f" in node '{node_name}'" if node_name else ""
        super().__init__(f"Budget exceeded [{dimension}]: {current} >= {limit}{node_part}")


# ── Budget tracker ─────────────────────────────────────────────────────────────


class BudgetTracker(BaseModel):
    """Tracks LLM spend for one investigation run.

    Frozen so that nodes return ``record_call()``'s new instance instead of
    mutating in place.  Safe for LangGraph state snapshots.

    Typical node pattern::

        budget = state["budget"]
        budget.check_can_call(tokens_in=500, tokens_out=200, estimated_cost=0.01)
        # ... call LLM ...
        budget = budget.record_call(tokens_in=523, tokens_out=187, cost=0.009)
        return {"budget": budget}
    """

    model_config = ConfigDict(frozen=True)

    config: BudgetConfig

    calls_made: int = Field(default=0, ge=0)
    tokens_input: int = Field(default=0, ge=0)
    tokens_output: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)

    # Set by ``enter_node()`` so per-node cost checks name the right node.
    current_node: str | None = None
    node_costs: dict[str, float] = Field(default_factory=dict)

    # ── Pre-flight check ───────────────────────────────────────────────────────

    def check_can_call(
        self,
        tokens_in: int = 0,
        tokens_out: int = 0,
        estimated_cost: float = 0.0,
    ) -> None:
        """Raise ``BudgetExceededError`` if this call would breach any cap.

        Checks in order: call count → per-call token limits → cumulative token
        limits → cumulative cost → per-node cost cap.

        The caller decides how to respond to ``BudgetExceededError`` based on
        ``config.on_budget_exceeded`` (fail_fast / return_partial / downgrade_lane).
        The tracker is intentionally policy-free.
        """
        cfg = self.config
        node = self.current_node

        if self.calls_made >= cfg.max_llm_calls:
            raise BudgetExceededError("llm_calls", self.calls_made, cfg.max_llm_calls, node)

        if tokens_in > cfg.max_tokens_per_call_input:
            raise BudgetExceededError(
                "tokens_per_call_input", tokens_in, cfg.max_tokens_per_call_input, node
            )
        if tokens_out > cfg.max_tokens_per_call_output:
            raise BudgetExceededError(
                "tokens_per_call_output", tokens_out, cfg.max_tokens_per_call_output, node
            )

        if self.tokens_input + tokens_in > cfg.max_tokens_input:
            raise BudgetExceededError(
                "tokens_input",
                self.tokens_input + tokens_in,
                cfg.max_tokens_input,
                node,
            )
        if self.tokens_output + tokens_out > cfg.max_tokens_output:
            raise BudgetExceededError(
                "tokens_output",
                self.tokens_output + tokens_out,
                cfg.max_tokens_output,
                node,
            )

        if self.cost_usd + estimated_cost > cfg.max_cost_usd:
            raise BudgetExceededError(
                "cost_usd", self.cost_usd + estimated_cost, cfg.max_cost_usd, node
            )

        if node is not None:
            per_node_cap = cfg.per_node_max_cost_usd.get(node)
            if per_node_cap is not None:
                node_spent = self.node_costs.get(node, 0.0)
                if node_spent + estimated_cost > per_node_cap:
                    raise BudgetExceededError(
                        f"node_cost:{node}", node_spent + estimated_cost, per_node_cap, node
                    )

    # ── Post-call accounting ───────────────────────────────────────────────────

    def record_call(
        self,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> BudgetTracker:
        """Return a new ``BudgetTracker`` with this call's usage recorded."""
        node = self.current_node
        updated_node_costs = dict(self.node_costs)
        if node is not None:
            updated_node_costs[node] = updated_node_costs.get(node, 0.0) + cost
        return self.model_copy(
            update={
                "calls_made": self.calls_made + 1,
                "tokens_input": self.tokens_input + tokens_in,
                "tokens_output": self.tokens_output + tokens_out,
                "cost_usd": self.cost_usd + cost,
                "node_costs": updated_node_costs,
            }
        )

    # ── Node transition ────────────────────────────────────────────────────────

    def enter_node(self, node_name: str) -> BudgetTracker:
        """Return a copy with ``current_node`` set, ready to check per-node caps."""
        updated = dict(self.node_costs)
        updated.setdefault(node_name, 0.0)
        return self.model_copy(update={"current_node": node_name, "node_costs": updated})

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def warning_triggered(self) -> bool:
        """True when cost has reached the configured warning threshold."""
        if self.config.max_cost_usd <= 0:
            return False
        pct = (self.cost_usd / self.config.max_cost_usd) * 100
        return pct >= self.config.warning_threshold_pct

    # ── Projection ────────────────────────────────────────────────────────────

    def to_run_metrics(
        self,
        entity_id: str,
        config_name: str,
        config_hash: str,
        elapsed_seconds: float = 0.0,
    ) -> RunMetrics:
        """Project accumulated counters into a ``RunMetrics`` for the workflow envelope."""
        return RunMetrics(
            entity_id=entity_id,
            config_name=config_name,
            config_hash=config_hash,
            total_llm_calls=self.calls_made,
            total_tokens_input=self.tokens_input,
            total_tokens_output=self.tokens_output,
            total_cost_usd=self.cost_usd,
            elapsed_seconds=elapsed_seconds,
            node_costs=dict(self.node_costs),
            budget_warning_triggered=self.warning_triggered,
        )


# ── Scope result ───────────────────────────────────────────────────────────────


class ScopeResult(BaseModel):
    """Output of ``scope_node``.

    ``should_proceed=False`` means the investigation was short-circuited (e.g.
    severity below ``PlanConfig.min_severity_to_run``).  The ``finalize_node``
    produces a ``PlanEnvelope(status="skipped", ...)`` in that case.
    """

    investigation_id: str = Field(min_length=1)
    should_proceed: bool
    skip_reason: str | None = None
    effective_severity: Literal["low", "medium", "high", "critical"]
    # Lane may be escalated from the config default when severity == "critical"
    # and kev_flagged == True.
    lane: LaneName


# ── Graph context ──────────────────────────────────────────────────────────────


class GraphContext(BaseModel):
    """Accumulated Neo4j query results from graph enrichment and relationship analysis.

    All fields default to empty/None so nodes can be added incrementally.
    """

    impact_count: int | None = Field(default=None, ge=0)
    relationship_chain_nodes: list[str] = Field(default_factory=list)
    related_entities: list[str] = Field(default_factory=list)
    # Each entry: {"entity_id": str, "resolution_type": str}
    similar_resolved: list[dict[str, str]] = Field(default_factory=list)
    dependent_entities: list[str] = Field(default_factory=list)
    cypher_queries_executed: int = Field(default=0, ge=0)
    hops_consumed: int = Field(default=0, ge=0)
    entity_context: dict | None = Field(default=None)


# ── LangGraph agent state ──────────────────────────────────────────────────────


class AgentState(TypedDict):
    """LangGraph state flowing between all nodes of the investigation workflow.

    Design rules:
    - ``envelope`` is stored as ``dict`` because the Postgres checkpointer
      serializes state to JSON; Pydantic models require ``model_dump(mode="json")`` first.
    - ``budget`` is ``BudgetTracker`` for type safety within a single execution.
    - ``plan_drafts`` is ``list[dict]`` — mutable working set during plan nodes.
    - ``workflow_envelope`` is the final output, set by the finalize node.
    """

    # ── Input ──────────────────────────────────────────────────────────────────
    envelope: dict[str, Any]  # Workflow input envelope
    config_name: str  # AgentDefinition name

    # ── Budget ─────────────────────────────────────────────────────────────────
    budget: BudgetTracker

    # ── Node outputs (accumulated as graph progresses) ─────────────────────────
    scope_result: ScopeResult | None
    graph_context: GraphContext | None
    plan_drafts: list[dict[str, Any]]  # Plan.model_dump() dicts

    # ── Final output ───────────────────────────────────────────────────────────
    plan_envelope: PlanEnvelope | None

    # ── Investigation creation (Postgres-backed) ─────────────────────────────
    entity_context: NotRequired[dict[str, Any] | None]  # Domain entity data
    entity_display_id: NotRequired[str | None]  # e.g. "FLT-7B3D"
    tenant_id: NotRequired[str | None]
    investigation_draft: NotRequired[dict[str, Any] | None]
    investigation_id_created: NotRequired[str | None]  # UUID after persist
    investigation_display_id: NotRequired[str | None]  # INV-XXXX after persist

    # ── Error accumulation ─────────────────────────────────────────────────────
    errors: list[str]
    current_node: str

    # ── Streaming (injected by runner, not serialized) ────────────────────────
    _step_emitter: NotRequired[Any]
    validation_passed: NotRequired[bool]
    validation_retries: NotRequired[int]
    started_at: NotRequired[float]
