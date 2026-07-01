"""WorkflowEnvelope, WorkflowPlan, WorkflowAction, RunMetrics.

Domain-agnostic output schemas for the investigation/workflow pipeline.
These are generic — domain-specific extensions can subclass or replace them.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CONFIG_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ── Workflow action ─────────────────────────────────────────────────────────────


class WorkflowAction(BaseModel):
    """A single action step within a workflow plan."""

    action_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10)
    owner: str = Field(min_length=1)
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"
    expected_outcome: str = Field(min_length=10)
    also_resolves: list[str] = Field(default_factory=list)
    rollback: str | None = Field(default=None, min_length=5)


# ── Workflow plan ───────────────────────────────────────────────────────────────


class WorkflowPlan(BaseModel):
    """A single plan within the workflow envelope."""

    plan_id: str = Field(min_length=1)
    classification: str = Field(min_length=1)
    is_primary: bool = False
    actions: Annotated[list[WorkflowAction], Field(min_length=1, max_length=5)]
    expected_outcome: str = Field(min_length=10)
    rationale: str = Field(min_length=10)

    @model_validator(mode="after")
    def _check_action_id_uniqueness(self) -> WorkflowPlan:
        ids = [a.action_id for a in self.actions]
        if len(set(ids)) != len(ids):
            seen: set[str] = set()
            dupes = [x for x in ids if x in seen or seen.add(x)]  # type: ignore[func-returns-value]
            raise ValueError(f"Plan '{self.plan_id}': duplicate action_id values: {dupes!r}")
        return self


# ── Run metrics ─────────────────────────────────────────────────────────────────


class RunMetrics(BaseModel):
    """Cost and performance telemetry from a workflow execution."""

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    config_name: str = Field(min_length=1)
    config_hash: str

    total_llm_calls: int = Field(default=0, ge=0)
    total_tokens_input: int = Field(default=0, ge=0)
    total_tokens_output: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    node_costs: dict[str, float] = Field(default_factory=dict)
    budget_warning_triggered: bool = False

    @field_validator("config_hash")
    @classmethod
    def _config_hash_format(cls, v: str) -> str:
        if not _CONFIG_HASH_RE.match(v):
            raise ValueError(f"config_hash must be 'sha256:<64 hex chars>', got: {v!r}")
        return v


# ── Workflow envelope ───────────────────────────────────────────────────────────


class PlanEnvelope(BaseModel):
    """Output envelope for a workflow execution.

    Contains the plans generated, comparison narrative, and run metrics.
    """

    envelope_id: str = Field(min_length=1)
    investigation_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)

    status: Literal["success", "partial", "skipped", "failed"]
    plans: list[WorkflowPlan] = Field(default_factory=list, max_length=4)
    comparison_narrative: str | None = None
    validation_warnings: list[str] = Field(default_factory=list)

    skip_reason: str | None = None
    failure_reason: str | None = None

    metrics: RunMetrics

    schema_version: str = "1.0"

    @model_validator(mode="after")
    def _validate_envelope_invariants(self) -> PlanEnvelope:
        self._check_status_plans_coherence()
        if self.plans:
            self._check_exactly_one_primary()
            self._check_comparison_narrative()
            self._check_plan_id_uniqueness()
        return self

    def _check_status_plans_coherence(self) -> None:
        if self.status in ("success", "partial") and not self.plans:
            raise ValueError(f"Plans must be non-empty when status is '{self.status}'.")
        if self.status == "skipped":
            if self.plans:
                raise ValueError("Plans must be empty when status is 'skipped'.")
            if not self.skip_reason:
                raise ValueError("skip_reason is required when status is 'skipped'.")
        if self.status == "failed":
            if self.plans:
                raise ValueError("Plans must be empty when status is 'failed'.")
            if not self.failure_reason:
                raise ValueError("failure_reason is required when status is 'failed'.")

    def _check_exactly_one_primary(self) -> None:
        count = sum(1 for p in self.plans if p.is_primary)
        if count != 1:
            raise ValueError(f"Must have exactly one primary plan, got {count}.")

    def _check_comparison_narrative(self) -> None:
        if len(self.plans) > 1 and (
            not self.comparison_narrative or len(self.comparison_narrative) < 10
        ):
            raise ValueError(
                "comparison_narrative is required (min 10 chars) when more than one plan is present."
            )

    def _check_plan_id_uniqueness(self) -> None:
        ids = [p.plan_id for p in self.plans]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate plan_id values: {ids!r}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanEnvelope:
        """Reconstruct from a plain dict."""
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return self.model_dump(mode="json")