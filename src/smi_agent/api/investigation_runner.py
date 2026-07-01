"""Investigation creation runner — domain-agnostic workflow.

The investigation pipeline is a LangGraph workflow that can be customized
by the domain. The default implementation delegates to domain providers
for data fetching and plan generation.
"""

from __future__ import annotations

import logging
from typing import Any

from smi_agent.api.models import CreateInvestigationRequest

logger = logging.getLogger(__name__)


async def run_investigation_creation(
    request: CreateInvestigationRequest,
    pg_client: Any,
    driver: Any,
    template_loader: Any,
    sse_streamer: Any,
    stream_id: str,
) -> None:
    """Run the investigation creation pipeline and stream results.

    This is a simplified domain-agnostic version. Domain-specific
    investigation logic (e.g., plan generation, graph enrichment) is
    delegated to the domain's providers.
    """
    import uuid as _uuid

    from smi_agent.config.loader import load_agent_definition
    from smi_agent.postgres_client.safe_executor import SafePostgresExecutor
    from smi_agent.utils.display_id import generate_display_id

    try:
        defn = load_agent_definition(request.agent_name)
        pg = SafePostgresExecutor(
            client=pg_client,
            allowed_queries=defn.postgres.allowed_queries,
            agent_name=defn.name,
        )

        investigation_id = _uuid.uuid4()
        display_id = generate_display_id("INV")

        # Publish initial step
        await sse_streamer.publish_step(stream_id, {
            "message": "Creating investigation...",
            "status": "in_progress",
        })

        # Insert investigation record
        await pg.execute(
            "insert_investigation",
            str(investigation_id),
            request.tenant_id,
            display_id,
            "INVESTIGATION",
            "DRAFT",
            "API",
            request.assignee_id or "",
            request.entity_id or "",
            request.entity_type or "entity",
            "seed",  # draft_source
            None,    # draft_drafted_at
            None,    # draft_comparison_narrative
        )

        await sse_streamer.publish_step(stream_id, {
            "message": "Investigation created successfully",
            "status": "completed",
            "investigation_id": str(investigation_id),
            "display_id": display_id,
        })

        # Publish result
        await sse_streamer.publish_response(stream_id, {
            "investigationId": str(investigation_id),
            "displayId": display_id,
            "state": "DRAFT",
            "message": "Investigation created. Configure domain-specific workflow nodes for plan generation.",
        })

    except Exception as exc:
        logger.exception("Investigation creation failed for stream %s", stream_id)
        await sse_streamer.publish_error(stream_id, f"Investigation creation failed: {type(exc).__name__}")

    finally:
        await sse_streamer.publish_done(stream_id)