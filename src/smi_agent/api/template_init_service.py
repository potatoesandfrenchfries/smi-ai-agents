"""Template init service — generates AI insights for entity context pages.

Delegates to the domain's ``TemplateProvider`` via ``DomainRegistry``.
"""

from __future__ import annotations

from typing import Any

from smi_agent.domain.registry import DomainRegistry


async def generate_template_content(
    pg_executor: Any,
    conversation_id: str,
    context_type: str | None,
    context_entity_id: str | None,
    context_label: str | None,
    tenant_id: str,
    on_step: Any = None,
) -> dict[str, Any]:
    """Generate template content for an entity context page.

    Delegates to the configured domain's TemplateProvider.
    """
    provider = DomainRegistry.template_provider()
    return await provider.generate_template(
        pg_executor=pg_executor,
        conversation_id=conversation_id,
        context_type=context_type,
        entity_id=context_entity_id,
        label=context_label,
        tenant_id=tenant_id,
        on_step=on_step,
    )