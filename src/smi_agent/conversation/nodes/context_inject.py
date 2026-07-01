"""context_inject_node — enrich ConversationState with page-scoped Neo4j data."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from smi_agent.config.models import AgentDefinition
from smi_agent.conversation.schemas import PageContext
from smi_agent.conversation.state import ConversationState
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.safe_executor import SafeCypherExecutor
from smi_agent.neo4j_client.templates import TemplateLoader
from smi_agent.streaming import get_step_emitter

logger = logging.getLogger(__name__)

# Generic fallback: context type → cypher template name.
# Domain-specific templates are resolved via GraphQueryProvider catalog.
_PAGE_CONTEXT_TEMPLATES: dict[str, str | None] = {
    "entity": "fetch_entity_context",
    "list": None,
    "dashboard": None,
    "general": None,
}


def make_context_inject_node(
    defn: AgentDefinition,
    driver: Neo4jDriver,
    template_loader: TemplateLoader,
):
    _allowed = list(defn.graph.allowed_cypher_templates)
    _agent_name = defn.name
    _graph_policy = defn.graph
    _page_context_types = set(defn.conversation.page_context_types)

    async def context_inject_node(
        state: ConversationState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        config = config or {}
        emitter = get_step_emitter(config)
        errors: list[str] = list(state.get("errors") or [])
        page_context: PageContext | None = state.get("page_context")

        if page_context is None:
            return {"page_context": None, "errors": errors, "current_node": "context_inject"}

        page_type = page_context.page_type

        # Check allowlist
        if page_type not in _page_context_types:
            logger.debug("context_inject: page_type %r not in allowlist, skipping", page_type)
            return {
                "page_context": page_context,
                "errors": errors,
                "current_node": "context_inject",
            }

        template_name = _PAGE_CONTEXT_TEMPLATES.get(page_type)
        if template_name is None:
            return {
                "page_context": page_context,
                "errors": errors,
                "current_node": "context_inject",
            }

        if template_name not in _allowed:
            logger.debug("context_inject: template %r not in allowed list, skipping", template_name)
            return {
                "page_context": page_context,
                "errors": errors,
                "current_node": "context_inject",
            }

        if not page_context.entity_id:
            return {
                "page_context": page_context,
                "errors": errors,
                "current_node": "context_inject",
            }

        await emitter.emit("context_inject", "in_progress", f"Loading {page_type} context...")

        executor = SafeCypherExecutor(
            driver=driver,
            allowed_templates=_allowed,
            agent_name=_agent_name,
            template_loader=template_loader,
            graph_policy=_graph_policy,
        )

        try:
            rows = await executor.run(template_name, entity_id=page_context.entity_id)
            snapshot = rows[0] if rows else {}
            enriched = PageContext(
                page_type=page_context.page_type,
                entity_id=page_context.entity_id,
                entity_snapshot=dict(snapshot) if snapshot else {},
                injected_at=page_context.injected_at,
            )
        except Exception as exc:
            err_msg = f"context_inject: enrichment failed for {page_type}: {type(exc).__name__}"
            logger.warning(err_msg)
            errors.append(err_msg)
            enriched = page_context

        await emitter.emit("context_inject", "completed", f"{page_type} context enriched")

        return {"page_context": enriched, "errors": errors, "current_node": "context_inject"}

    return context_inject_node
