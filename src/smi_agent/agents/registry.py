"""AgentRegistry — discovers and manages specialist agents.

Scans ``agent_definitions/specialist_*.yaml`` files at startup. For each:
- If a custom Python class is registered in ``_CUSTOM_SPECIALISTS``, use it
- Otherwise, wrap it in a ``GenericSpecialist`` that uses ``AgentRuntime``

Adding a new agent:
  1. Create ``agent_definitions/specialist_<name>.yaml``
  2. Create ``prompts/agents/<name>/system.j2``
  3. (Optional) Register a custom class in ``_CUSTOM_SPECIALISTS``
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from smi_agent.agents.response import StructuredResponse
from smi_agent.agents.runtime import AgentRuntime
from smi_agent.agents.specialists.base import BaseSpecialist
from smi_agent.config.loader import load_agent_definition
from smi_agent.conversation.tools.registry import ToolRegistry
from smi_agent.domain.registry import DomainRegistry
from smi_agent.llm.prompts import PromptLoader
from smi_agent.llm.router import LLMRouter
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.safe_executor import SafeCypherExecutor
from smi_agent.neo4j_client.templates import TemplateLoader
from smi_agent.observability.logging import get_planner_trace_logger
from smi_agent.postgres_client.safe_executor import SafePostgresExecutor
from smi_agent.streaming import StepEmitter

logger = logging.getLogger(__name__)
trace_logger = get_planner_trace_logger()


# ── Custom specialist class mapping ──────────────────────────────────────────
# Add entries here when a specialist needs custom Python logic beyond
# the generic AgentRuntime. The key is the YAML file name (without extension).

def _build_custom_map() -> dict[str, type]:
    # Import custom specialists here to avoid circular imports.
    # Each domain can add its own custom specialists to this map.
    from smi_agent.agents.specialists.flight import FlightSpecialist
    from smi_agent.agents.specialists.hotel import HotelSpecialist
    from smi_agent.agents.specialists.restaurant import RestaurantSpecialist

    return {
        "specialist_flight": FlightSpecialist,
        "specialist_hotel": HotelSpecialist,
        "specialist_restaurant": RestaurantSpecialist,
    }


# ── Generic specialist wrapper ───────────────────────────────────────────────


class GenericSpecialist(BaseSpecialist):
    """Wraps any YAML-defined agent with the standard AgentRuntime."""

    def __init__(
        self,
        agent_def_name: str,
        neo4j_driver: Neo4jDriver,
        template_loader: TemplateLoader,
        pg_client: Any | None = None,
        specialist_registry: AgentRegistry | None = None,
    ) -> None:
        self._def_name = agent_def_name
        self._defn = load_agent_definition(agent_def_name)
        self._driver = neo4j_driver
        self._template_loader = template_loader
        self._pg_client = pg_client
        self._prompt_loader = PromptLoader(self._defn.llm.prompt_templates_dir)
        # Sibling registry, so this specialist's tool-calling loop can expose
        # other specialists (e.g. flight/hotel/restaurant) as ask_<name> tools
        # — this is what lets the "planner" specialist dynamically decide
        # which specialists to invoke instead of following a fixed pipeline.
        # Passed in by AgentRegistry._discover() as `self` — its own
        # `_specialists` dict isn't fully populated yet at construction time,
        # but that's fine: we only read it lazily inside run(), by which
        # point discovery has long since finished.
        self._specialist_registry = specialist_registry

    @property
    def name(self) -> str:
        return self._def_name.replace("specialist_", "")

    @property
    def description(self) -> str:
        return self._defn.description or f"Specialist agent: {self.name}"

    async def run(
        self,
        query: str,
        context: dict[str, Any],
        step_emitter: StepEmitter,
    ) -> StructuredResponse:
        # Build executors
        cypher_exec = None
        if self._defn.graph.allowed_cypher_templates:
            cypher_exec = SafeCypherExecutor(
                driver=self._driver,
                allowed_templates=self._defn.graph.allowed_cypher_templates,
                agent_name=self._defn.name,
                template_loader=self._template_loader,
                graph_policy=self._defn.graph,
            )

        pg_exec = None
        if self._pg_client is not None:
            pg_exec = SafePostgresExecutor(
                client=self._pg_client,
                allowed_queries=self._defn.postgres.allowed_queries,
                agent_name=self._defn.name,
            )

        # Sibling specialists this agent may dynamically delegate to, exposed
        # as ask_<name> tools (e.g. the "planner" specialist gets ask_flight,
        # ask_hotel, ask_restaurant). Excludes self to avoid a specialist
        # recursively calling itself. Only populated when a sibling registry
        # was supplied — most specialists don't need this.
        sibling_specialists = (
            [s for s in self._specialist_registry.all_specialists() if s.name != self.name]
            if self._specialist_registry is not None
            else []
        )

        tc = self._defn.tool_calling
        registry = ToolRegistry(
            cypher_executor=cypher_exec,
            postgres_executor=pg_exec,
            template_loader=self._template_loader,
            enabled_tools=tc.enabled_tools or None,
            specialists=sibling_specialists,
            specialist_context=context,
            step_emitter=step_emitter,
        )

        if sibling_specialists:
            trace_logger.info(
                "[%s] dynamic tools available: %s",
                self.name, ", ".join(f"ask_{s.name}" for s in sibling_specialists),
            )

        tenant_id = context.get("tenant_id", "00000000-0000-0000-0000-000000000001")
        entity_id = context.get("entity_id", "")

        # Merge domain entity labels into the prompt context
        domain_schema = DomainRegistry.schema()
        system_prompt = self._prompt_loader.render_system(
            "system",
            {
                "agent_name": self._defn.name,
                "tenant_id": tenant_id,
                "entity_id": entity_id,
                "available_tools": ", ".join(registry.tool_names()),
                "entity_labels": ", ".join(domain_schema.entity_labels),
                "relationship_types": ", ".join(domain_schema.relationship_types),
            },
        )

        router = LLMRouter(lane=self._defn.llm.lane, temperature=self._defn.llm.temperature)

        runtime = AgentRuntime(
            agent_name=self.name,
            tool_registry=registry,
            llm_router=router,
            system_prompt=system_prompt,
            step_emitter=step_emitter,
            max_iterations=tc.max_tool_iterations,
            max_llm_calls=tc.max_llm_calls,
        )

        return await runtime.run(user_message=query, context=context)


# ── Registry ─────────────────────────────────────────────────────────────────


class AgentRegistry:
    """Discovers and manages specialist agents from YAML configs.

    Usage:
        registry = AgentRegistry(neo4j_driver, template_loader, pg_client)
        specialist = registry.get("travel_explorer")
        result = await specialist.run("Show flight routes", context, emitter)
    """

    def __init__(
        self,
        neo4j_driver: Neo4jDriver,
        template_loader: TemplateLoader,
        pg_client: Any | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self._driver = neo4j_driver
        self._loader = template_loader
        self._pg_client = pg_client
        self._redis_client = redis_client
        self._specialists: dict[str, BaseSpecialist] = {}
        self._discover()

    def _discover(self) -> None:
        """Scan agent_definitions/ for specialist_*.yaml and instantiate."""
        custom_map = _build_custom_map()
        defs_dir = Path(os.environ.get("SMI_DEFINITIONS_DIR", "agent_definitions"))

        if not defs_dir.exists():
            logger.warning("Agent definitions dir not found: %s", defs_dir)
            return

        for yaml_file in sorted(defs_dir.glob("specialist_*.yaml")):
            def_name = yaml_file.stem
            short_name = def_name.replace("specialist_", "")

            try:
                if def_name in custom_map:
                    cls = custom_map[def_name]
                    specialist = cls(
                        neo4j_driver=self._driver,
                        template_loader=self._loader,
                        pg_client=self._pg_client,
                        redis_client=self._redis_client,
                    )
                else:
                    specialist = GenericSpecialist(
                        agent_def_name=def_name,
                        neo4j_driver=self._driver,
                        template_loader=self._loader,
                        pg_client=self._pg_client,
                        specialist_registry=self,
                    )

                self._specialists[short_name] = specialist
                logger.info("Registered specialist: %s (%s)", short_name, type(specialist).__name__)
                trace_logger.info("REGISTER specialist=%s impl=%s", short_name, type(specialist).__name__)

            except Exception:
                logger.exception("Failed to load specialist %s", def_name)

        logger.info("AgentRegistry: %d specialists registered", len(self._specialists))

    def get(self, name: str) -> BaseSpecialist | None:
        """Get a specialist by short name (e.g., 'travel_explorer')."""
        return self._specialists.get(name)

    def all_specialists(self) -> list[BaseSpecialist]:
        """Return all registered specialists."""
        return list(self._specialists.values())

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI function tool definitions for all specialists."""
        return [s.as_tool_definition() for s in self._specialists.values()]

    def specialist_names(self) -> list[str]:
        """Return names of all registered specialists."""
        return list(self._specialists.keys())