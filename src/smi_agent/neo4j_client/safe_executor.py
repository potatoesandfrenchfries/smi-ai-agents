"""SafeCypherExecutor — the only path through which Neo4j queries run.

Security invariant
------------------
``allow_raw_cypher=False`` (default and enforced in every AgentDefinition) means
the LLM **never** authors Cypher.  Every query arrives here as a *template name*
looked up against two independent allowlists:

1. **Agent allowlist** — ``AgentDefinition.graph.allowed_cypher_templates``
2. **Template allowlist** — ``CypherTemplate.allowed_agents``

Both must pass or the query is refused with a ``PolicyViolationError`` and a
policy-violation log entry.  This double-gate means a compromised agent
definition cannot unilaterally enable a dangerous template.

Budget enforcement
------------------
The executor also tracks ``queries_executed`` and ``hops_consumed`` against
``GraphPolicy.max_queries`` and ``GraphPolicy.hop_budget``.  Exceeding either
raises ``QueryBudgetExceededError``.

Usage (inside a LangGraph node, M5)::

    executor = SafeCypherExecutor(
        driver=driver,
        allowed_templates=defn.graph.allowed_cypher_templates,
        agent_name=defn.name,
        template_loader=loader,
        graph_policy=defn.graph,
    )
    rows = await executor.run("fetch_exposure_context", exposure_id="EXP-001")
"""

from __future__ import annotations

import logging
from typing import Any

from smi_agent.config.models import GraphPolicy
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.templates import (
    CypherTemplate,
    TemplateLoader,
)

logger = logging.getLogger(__name__)


# ── Errors ─────────────────────────────────────────────────────────────────────


class PolicyViolationError(PermissionError):
    """Raised when a template is blocked by the two-layer allowlist."""

    def __init__(self, reason: str, template_name: str, agent_name: str) -> None:
        self.template_name = template_name
        self.agent_name = agent_name
        super().__init__(reason)


class QueryBudgetExceededError(RuntimeError):
    """Raised when max_queries or hop_budget is exhausted."""

    def __init__(self, dimension: str, current: int, limit: int) -> None:
        self.dimension = dimension
        self.current = current
        self.limit = limit
        super().__init__(f"Graph query budget exceeded [{dimension}]: {current} >= {limit}")


# ── Executor ───────────────────────────────────────────────────────────────────


class SafeCypherExecutor:
    """Executes allowlisted Cypher templates via the Neo4j async driver.

    One executor instance is created per investigation run.  The ``queries_executed``
    and ``hops_consumed`` counters accumulate across all calls and are synced to
    ``GraphContext`` by the calling node after each query.

    Args:
        driver: ``Neo4jDriver`` (or a mock with the same ``run_query`` interface).
        allowed_templates: List of template names from
            ``AgentDefinition.graph.allowed_cypher_templates``.
        agent_name: The agent definition name, checked against
            ``CypherTemplate.allowed_agents``.
        template_loader: Pre-loaded ``TemplateLoader`` instance.
        graph_policy: ``GraphPolicy`` from the agent definition (max_queries,
            hop_budget).
    """

    def __init__(
        self,
        driver: Neo4jDriver,
        allowed_templates: list[str],
        agent_name: str,
        template_loader: TemplateLoader,
        graph_policy: GraphPolicy,
    ) -> None:
        self._driver = driver
        self._allowed_templates: frozenset[str] = frozenset(allowed_templates)
        self._agent_name = agent_name
        self._loader = template_loader
        self._policy = graph_policy

        # Mutable counters — updated on each successful query.
        self.queries_executed: int = 0
        self.hops_consumed: int = 0

    async def run(
        self,
        template_name: str,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Execute a named template with the given parameters.

        Steps:
        1. Verify template is in agent's allowed_cypher_templates.
        2. Verify agent is in template's allowed_agents.
        3. Check query/hop budget.
        4. Render template (clamp + interpolate).
        5. Execute via driver with cost_class timeout.
        6. Update counters.

        Returns:
            List of record dicts from Neo4j.

        Raises:
            PolicyViolationError: Allowlist check failed.
            QueryBudgetExceededError: Budget exhausted.
            TemplateNotFoundError: Template not registered.
            TemplateParameterError: Missing/invalid parameter.
            Neo4jDriverError: Driver-level failure.
        """
        # ── Step 1: Agent-level allowlist ──────────────────────────────────────
        if template_name not in self._allowed_templates:
            logger.warning(
                "POLICY_VIOLATION: agent '%s' attempted to run template '%s' "
                "which is not in its allowed_cypher_templates.",
                self._agent_name,
                template_name,
            )
            raise PolicyViolationError(
                f"Template '{template_name}' is not in the allowed_cypher_templates "
                f"for agent '{self._agent_name}'.",
                template_name=template_name,
                agent_name=self._agent_name,
            )

        # ── Step 2: Load template ──────────────────────────────────────────────
        template: CypherTemplate = self._loader[template_name]

        # ── Step 3: Template-level allowed_agents check ────────────────────────
        if not self._agent_is_allowed(template):
            logger.warning(
                "POLICY_VIOLATION: template '%s' does not permit agent '%s'. allowed_agents=%s",
                template_name,
                self._agent_name,
                template.allowed_agents,
            )
            raise PolicyViolationError(
                f"Template '{template_name}' does not permit agent '{self._agent_name}'.",
                template_name=template_name,
                agent_name=self._agent_name,
            )

        # ── Step 4: Budget check ───────────────────────────────────────────────
        self._check_query_budget()

        # ── Step 5: Render (clamp + interpolate) ──────────────────────────────
        query_text, cypher_params = template.render(dict(params))

        # ── Step 6: Execute ────────────────────────────────────────────────────
        logger.debug(
            "Executing template '%s' (cost_class=%s timeout=%.1fs params=%s)",
            template_name,
            template.cost_class,
            template.timeout,
            list(cypher_params),
        )
        records = await self._driver.run_query(
            cypher=query_text,
            parameters=cypher_params,
            timeout=template.timeout,
        )

        # ── Step 7: Update counters ────────────────────────────────────────────
        self.queries_executed += 1
        self.hops_consumed += self._estimate_hops(template_name, params)

        return records

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _agent_is_allowed(self, template: CypherTemplate) -> bool:
        return "*" in template.allowed_agents or self._agent_name in template.allowed_agents

    def _check_query_budget(self) -> None:
        if self.queries_executed >= self._policy.max_queries:
            raise QueryBudgetExceededError(
                "max_queries", self.queries_executed, self._policy.max_queries
            )
        if self.hops_consumed >= self._policy.hop_budget:
            raise QueryBudgetExceededError(
                "hop_budget", self.hops_consumed, self._policy.hop_budget
            )

    def _estimate_hops(self, template_name: str, params: dict[str, Any]) -> int:
        """Best-effort hop-cost estimate for the query budget counter.

        Uses the clamped max_depth / max_hops param if present, otherwise 1.
        """
        for key in ("max_depth", "max_hops"):
            if key in params:
                try:
                    return int(params[key])
                except (TypeError, ValueError):
                    pass
        return 1
