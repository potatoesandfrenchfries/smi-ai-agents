"""OpenTelemetry setup: one span per LangGraph node, per Cypher query, per LLM call.

Initialise the tracer once at worker startup by calling ``configure_tracing()``.
Then use the context-manager helpers to instrument individual operations::

    from smi_agent.observability.tracing import node_span, query_span, llm_span

    async with node_span("plan_generation", investigation_id="inv-001") as span:
        span.set_attribute("exposure_id", "EXP-1621")
        ...

If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set, spans are emitted to a
``NoOpSpanExporter`` so the helpers remain safe to call in all environments.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

_TRACER_NAME = "smi_agent"
_tracer = None


def configure_tracing(
    service_name: str = "smi-investigation-agent",
    endpoint: str | None = None,
) -> None:
    """Set up the OTel tracer provider.

    If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set and ``endpoint`` is not
    provided, falls back to a ``NoOpSpanExporter`` (zero overhead).
    """
    global _tracer  # noqa: PLW0603

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        otlp_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info("OTel tracing: exporting to %s", otlp_endpoint)
            except Exception as exc:
                logger.warning("OTel OTLP exporter init failed: %s — using NoOp", exc)
        else:
            from opentelemetry.sdk.trace.export import NoOpSpanExporter

            provider.add_span_processor(SimpleSpanProcessor(NoOpSpanExporter()))
            logger.debug("OTel tracing: NoOp exporter (OTEL_EXPORTER_OTLP_ENDPOINT not set)")

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(_TRACER_NAME)
        logger.info("OTel tracer configured: service=%s", service_name)

    except ImportError:
        logger.warning(
            "opentelemetry-sdk not installed — tracing disabled. "
            "Install it: pip install opentelemetry-sdk"
        )
        _tracer = None


def _get_tracer():
    """Return the tracer, lazily initialising if not yet configured."""
    if _tracer is None:
        configure_tracing()
    return _tracer


# ── Span context managers ──────────────────────────────────────────────────────


@asynccontextmanager
async def node_span(
    node_name: str,
    investigation_id: str | None = None,
    **attributes: Any,
) -> AsyncIterator[Any]:
    """Async context manager for a LangGraph node span."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(f"node.{node_name}") as span:
        if investigation_id:
            span.set_attribute("investigation_id", investigation_id)
        span.set_attribute("node_name", node_name)
        for k, v in attributes.items():
            span.set_attribute(k, str(v))
        yield span


@asynccontextmanager
async def query_span(
    template_name: str,
    investigation_id: str | None = None,
    **attributes: Any,
) -> AsyncIterator[Any]:
    """Async context manager for a Cypher query span."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(f"cypher.{template_name}") as span:
        span.set_attribute("db.system", "neo4j")
        span.set_attribute("template_name", template_name)
        if investigation_id:
            span.set_attribute("investigation_id", investigation_id)
        for k, v in attributes.items():
            span.set_attribute(k, str(v))
        yield span


@asynccontextmanager
async def llm_span(
    operation: str,
    model: str | None = None,
    lane: str | None = None,
    investigation_id: str | None = None,
    **attributes: Any,
) -> AsyncIterator[Any]:
    """Async context manager for an LLM call span."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(f"llm.{operation}") as span:
        if model:
            span.set_attribute("llm.model", model)
        if lane:
            span.set_attribute("llm.lane", lane)
        if investigation_id:
            span.set_attribute("investigation_id", investigation_id)
        for k, v in attributes.items():
            span.set_attribute(k, str(v))
        yield span
