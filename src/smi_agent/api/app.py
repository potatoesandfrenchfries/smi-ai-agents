"""FastAPI application for the SMI Agent conversation interface."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from smi_agent.api.conversation_runner import restore_session_from_postgres, run_conversation_turn
from smi_agent.api.conversation_service import (
    count_active_conversations,
    create_conversation,
    get_conversation,
    get_messages,
    list_conversations,
)
from smi_agent.api.investigation_runner import run_investigation_creation
from smi_agent.api.models import (
    ChatRequest,
    ConversationChatRequest,
    CreateConversationRequest,
    CreateInvestigationRequest,
    HistoryResponse,
    JsonPatchOp,
)
from smi_agent.config.loader import load_agent_definition
from smi_agent.conversation.graph import build_conversation_graph
from smi_agent.conversation.queue import make_stomp_consumer
from smi_agent.conversation.schemas import PageContext, SessionMeta
from smi_agent.conversation.session_store import RedisSessionStore
from smi_agent.conversation.sse import SSEStreamer
from smi_agent.conversation.worker import run_conversation_worker
from smi_agent.domain.registry import DomainRegistry
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.templates import TemplateLoader

logger = logging.getLogger(__name__)


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: validate X-API-Key header when API_KEY env var is set."""
    expected = os.environ.get("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _extract_tenant_id(x_auth_tenant_id: str | None = Header(default=None)) -> str | None:
    """Extract tenant ID from the ``X-Auth-Tenant-Id`` header."""
    return x_auth_tenant_id


def _require_user_id(x_auth_user_id: str | None = Header(default=None)) -> str:
    """Extract and require user ID from the ``X-Auth-User-Id`` header."""
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="X-Auth-User-Id header is required")
    return x_auth_user_id


def _get_pg_executor(agent_name: str = "conversation"):
    """Get SafePostgresExecutor from app state. Raises 503 if unavailable."""
    pg_client = app.state.pg_client
    if pg_client is None:
        raise HTTPException(status_code=503, detail="Postgres not configured")
    from smi_agent.postgres_client.safe_executor import SafePostgresExecutor

    defn = load_agent_definition(agent_name)
    return SafePostgresExecutor(
        client=pg_client,
        allowed_queries=defn.postgres.allowed_queries,
        agent_name=defn.name,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    # Configure domain from env var
    domain_module = os.environ.get("SMI_DOMAIN", "")
    if domain_module:
        _configure_domain(domain_module)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_socket_timeout = int(os.environ.get("REDIS_SOCKET_TIMEOUT", "300"))
    redis_connect_timeout = int(os.environ.get("REDIS_CONNECT_TIMEOUT", "10"))
    redis_client = aioredis.from_url(
        redis_url,
        decode_responses=False,
        socket_timeout=redis_socket_timeout,
        socket_connect_timeout=redis_connect_timeout,
    )

    driver = Neo4jDriver.from_env()
    template_loader = TemplateLoader()
    session_store = RedisSessionStore(redis_client)
    sse_streamer = SSEStreamer(redis_client)

    # Pre-build the default conversation graph at startup
    agent_name = os.environ.get("SMI_CONVERSATION_AGENT_NAME", "conversation")
    defn = load_agent_definition(agent_name)
    graph = build_conversation_graph(defn=defn, driver=driver, template_loader=template_loader)

    # Optional Postgres client
    pg_client = None
    pg_url = os.environ.get("SMI_POSTGRES_URL", "")
    if pg_url:
        try:
            from smi_agent.postgres_client.client import PostgresClient

            pg_client = PostgresClient(pg_url)
            await pg_client.warmup()
            logger.info("Postgres client created and pool warmed up")
        except Exception:
            logger.warning(
                "Failed to create Postgres client — persistence disabled", exc_info=True
            )

    app.state.driver = driver
    app.state.template_loader = template_loader
    app.state.session_store = session_store
    app.state.sse_streamer = sse_streamer
    app.state.pg_client = pg_client
    app.state.graphs: dict[str, Any] = {agent_name: graph}
    app.state.background_tasks: set[asyncio.Task] = set()

    # Multi-agent supervisor framework
    from smi_agent.agents.registry import AgentRegistry
    from smi_agent.agents.supervisor import SupervisorAgent

    agent_registry = AgentRegistry(
        neo4j_driver=driver,
        template_loader=template_loader,
        pg_client=pg_client,
        redis_client=redis_client,
    )
    app.state.agent_registry = agent_registry
    app.state.supervisor = SupervisorAgent(agent_registry=agent_registry)

    # Optionally start the queue worker
    worker_task: asyncio.Task | None = None
    activemq_url = os.environ.get("ACTIVEMQ_URL", "")
    if activemq_url:
        try:
            consumer = make_stomp_consumer(
                url=activemq_url,
                destination=os.environ.get("ACTIVEMQ_DESTINATION", "/queue/smi.chat"),
                credentials=(
                    os.environ.get("ACTIVEMQ_USERNAME", "admin"),
                    os.environ.get("ACTIVEMQ_PASSWORD", "admin"),
                ),
            )
            worker_task = asyncio.create_task(
                run_conversation_worker(consumer, graph, session_store, sse_streamer),
                name="conversation-queue-worker",
            )
            app.state.background_tasks.add(worker_task)
            worker_task.add_done_callback(app.state.background_tasks.discard)
            logger.info("Queue worker started for %s", activemq_url)
        except Exception:
            logger.exception("Failed to start queue worker — continuing without it")

    yield

    if worker_task is not None:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    if pg_client is not None:
        await pg_client.close()
    await driver.close()
    await redis_client.aclose()


def _configure_domain(module_path: str) -> None:
    """Import and configure a domain module at startup."""
    try:
        import importlib

        domain_mod = importlib.import_module(module_path)
        DomainRegistry.configure(domain_mod)
        logger.info("Domain configured: %s", module_path)
    except Exception:
        logger.exception("Failed to configure domain: %s", module_path)


app = FastAPI(title="SMI Agent Conversation API", lifespan=lifespan)


def _get_or_build_graph(app: FastAPI, agent_name: str) -> Any:
    if agent_name not in app.state.graphs:
        defn = load_agent_definition(agent_name)
        app.state.graphs[agent_name] = build_conversation_graph(
            defn=defn,
            driver=app.state.driver,
            template_loader=app.state.template_loader,
            pg_client=app.state.pg_client,
        )
    return app.state.graphs[agent_name]


def _resolve_agent_name(context_type: str | None, entity_id: str | None, label: str | None) -> str:
    """Resolve agent name via domain EntityResolver."""
    resolver = DomainRegistry.entity_resolver()
    return resolver.resolve_agent_name(
        context_type=context_type,
        entity_id=entity_id,
        label=label,
    )


@app.post("/chat")
async def chat(
    request: ChatRequest,
    _: None = Depends(_require_api_key),
) -> StreamingResponse:
    session_store: RedisSessionStore = app.state.session_store
    sse_streamer: SSEStreamer = app.state.sse_streamer

    loaded = await session_store.load(request.session_id)
    if loaded is None:
        defn = load_agent_definition(request.agent_name)
        meta = SessionMeta(
            session_id=request.session_id,
            agent_name=request.agent_name,
            ttl_seconds=defn.conversation.session_ttl_seconds,
        )
        history: list[Any] = []
    else:
        meta, history = loaded

    page_ctx: PageContext | None = None
    if request.page_context:
        pc = request.page_context
        resolver = DomainRegistry.entity_resolver()
        valid_types = resolver.valid_page_types()
        page_ctx = PageContext(
            page_type=pc.page_type if pc.page_type in valid_types else "general",
            entity_id=pc.entity_id,
        )

    state = {
        "session_id": request.session_id,
        "agent_name": request.agent_name,
        "user_message": request.user_message,
        "page_context": page_ctx,
        "history": history,
        "errors": [],
        "should_compact": False,
    }

    graph = _get_or_build_graph(app, request.agent_name)

    pubsub = await sse_streamer.open_channel(request.session_id)

    task = asyncio.create_task(
        run_conversation_turn(
            state=state,
            graph=graph,
            session_store=session_store,
            sse_streamer=sse_streamer,
            session_meta=meta,
        )
    )
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)

    return StreamingResponse(
        sse_streamer.stream_channel(pubsub, request.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    _: None = Depends(_require_api_key),
) -> HistoryResponse:
    session_store: RedisSessionStore = app.state.session_store
    loaded = await session_store.load(session_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")
    meta, history = loaded
    return HistoryResponse(
        session_id=session_id,
        messages=history,
        meta=meta.model_dump(mode="json"),
    )


@app.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    _: None = Depends(_require_api_key),
) -> None:
    session_store: RedisSessionStore = app.state.session_store
    await session_store.delete(session_id)


# ── Conversation API (/api/v1/conversations) ──────────────────────────────────


@app.post("/api/v1/conversations", status_code=201)
async def create_conversation_endpoint(
    request: CreateConversationRequest,
    user_id: str = Depends(_require_user_id),
    tenant_id: str | None = Depends(_extract_tenant_id),
):
    """Create a new conversation."""
    pg = _get_pg_executor()
    tid = tenant_id or "default"

    defn = load_agent_definition(request.agentName)
    active_count = await count_active_conversations(pg, user_id, tid)
    max_active = defn.conversation.max_active_conversations_per_user
    if active_count >= max_active:
        raise HTTPException(
            status_code=409,
            detail=f"Maximum active conversations reached ({max_active}). Close an existing conversation to start a new one.",
        )

    resolver = DomainRegistry.entity_resolver()
    valid_types = resolver.valid_page_types()

    # Validate context requires entityId for scoped types
    if (
        request.context
        and request.context.type not in ("general", "dashboard")
        and request.context.type in valid_types
        and not request.context.entityId
        and not request.context.label
        and resolver.entity_id_required(request.context.type)
    ):
        raise HTTPException(
            status_code=422,
            detail=f"context.entityId or label is required for type={request.context.type}",
        )

    ctx = request.context.model_dump() if request.context else None

    resolved_agent = _resolve_agent_name(
        context_type=ctx.get("type") if ctx else None,
        entity_id=ctx.get("entityId") if ctx else None,
        label=ctx.get("label") if ctx else None,
    )
    resolved_defn = load_agent_definition(resolved_agent)

    result = await create_conversation(
        pg,
        tenant_id=tid,
        user_id=user_id,
        agent_name=resolved_agent,
        context=ctx,
        ceiling_max_messages=resolved_defn.conversation.ceiling_max_messages,
        ceiling_max_tokens=resolved_defn.conversation.ceiling_max_tokens,
    )
    return result


@app.post("/api/v1/conversations/{conversation_id}/init")
async def init_conversation_template_endpoint(
    conversation_id: str,
    user_id: str = Depends(_require_user_id),
    tenant_id: str | None = Depends(_extract_tenant_id),
) -> StreamingResponse:
    """Generate AI insights template content via SSE with reasoning steps."""
    import json as _json

    pg = _get_pg_executor()
    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    pg = _get_pg_executor(conv["agentName"])
    tid = conv.get("tenantId") or tenant_id or "00000000-0000-0000-0000-000000000001"

    # If template already exists, return it immediately
    template_rows = await pg.run("fetch_conversation_template", conversation_id, user_id)
    if template_rows and template_rows[0].get("template_content"):
        cached = template_rows[0]["template_content"]

        async def stream_cached():
            yield f"data: {_json.dumps({'type': 'template', 'data': cached})}\n\n"
            yield f"data: {_json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(
            stream_cached(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    ctx = conv.get("context") or {}

    async def stream_init():
        import asyncio as _aio

        queue: _aio.Queue = _aio.Queue()

        async def on_step(msg: str):
            await queue.put({"type": "step", "message": msg})

        async def run_generation():
            try:
                template_provider = DomainRegistry.template_provider()
                template = await template_provider.generate_template(
                    pg_executor=pg,
                    conversation_id=conversation_id,
                    context_type=ctx.get("type"),
                    entity_id=ctx.get("entityId"),
                    label=ctx.get("label"),
                    tenant_id=tid,
                    on_step=on_step,
                )
            except Exception:
                logger.exception("Template init failed for conversation %s", conversation_id)
                template = {
                    "summary": "",
                    "recommendation": "",
                    "synthesisBullets": [],
                    "contextChips": [],
                    "suggestions": [],
                    "inputPlaceholder": f"Ask about {ctx.get('label') or 'this page'}...",
                }
            await queue.put({"type": "template", "data": template})
            await queue.put({"type": "done"})

        task = _aio.create_task(run_generation())

        while True:
            event = await queue.get()
            yield f"data: {_json.dumps(event)}\n\n"
            if event.get("type") == "done":
                break

        await task

    return StreamingResponse(
        stream_init(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v1/conversations/{conversation_id}/chat")
async def conversation_chat_endpoint(
    conversation_id: str,
    request: ConversationChatRequest,
    user_id: str = Depends(_require_user_id),
) -> StreamingResponse:
    """Send a message within a conversation (SSE stream with dual-write)."""
    pg = _get_pg_executor()

    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv["status"] == "CEILING_HIT":
        raise HTTPException(
            status_code=409,
            detail={
                "type": "ceiling_exceeded",
                "detail": "Conversation limit reached. Start a new conversation.",
                "ceiling": conv["ceiling"],
            },
        )
    if conv["status"] != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail={
                "type": "conversation_closed",
                "detail": f"Conversation is {conv['status']}. Start a new conversation.",
            },
        )

    ceiling = conv["ceiling"]
    if ceiling["messagesUsed"] >= ceiling["maxMessages"]:
        raise HTTPException(
            status_code=409,
            detail={"type": "ceiling_exceeded", "detail": "Message limit reached", "ceiling": ceiling},
        )
    if ceiling["tokensUsed"] >= ceiling["maxTokens"]:
        raise HTTPException(
            status_code=409,
            detail={"type": "ceiling_exceeded", "detail": "Token limit reached", "ceiling": ceiling},
        )

    session_store: RedisSessionStore = app.state.session_store
    sse_streamer: SSEStreamer = app.state.sse_streamer
    session_id = conversation_id

    loaded = await session_store.load(session_id)
    if loaded is not None:
        meta, history = loaded
    else:
        _, history = await restore_session_from_postgres(pg, conversation_id, user_id)
        defn = load_agent_definition(conv["agentName"])
        meta = SessionMeta(
            session_id=session_id,
            agent_name=conv["agentName"],
            ttl_seconds=defn.conversation.session_ttl_seconds,
            turn_count=conv["ceiling"]["messagesUsed"] // 2,
            total_tokens_used=conv["ceiling"]["tokensUsed"],
            compaction_count=conv.get("compactionCount", 0),
        )

    resolver = DomainRegistry.entity_resolver()
    valid_types = resolver.valid_page_types()

    page_ctx: PageContext | None = None
    if request.pageContext:
        pc = request.pageContext
        page_ctx = PageContext(
            page_type=pc.page_type if pc.page_type in valid_types else "general",
            entity_id=pc.entity_id,
        )
    elif conv.get("context"):
        ctx = conv["context"]
        page_type = ctx["type"] if ctx["type"] in valid_types else "general"
        page_ctx = PageContext(
            page_type=page_type,
            entity_id=ctx.get("entityId"),
        )

    if page_ctx and conv.get("templateContent"):
        tc = conv["templateContent"]
        snapshot_text = ""
        if isinstance(tc, dict):
            if tc.get("summary"):
                snapshot_text += f"AI Summary: {tc['summary']}\n"
            if tc.get("recommendation"):
                snapshot_text += f"Recommendation: {tc['recommendation']}\n"
            for b in tc.get("synthesisBullets", []):
                snapshot_text += f"- {b.get('text', '')}\n"
        if snapshot_text:
            page_ctx = PageContext(
                page_type=page_ctx.page_type,
                entity_id=page_ctx.entity_id,
                entity_snapshot={"context_summary": snapshot_text.strip()},
            )

    state = {
        "session_id": session_id,
        "agent_name": conv["agentName"],
        "user_message": request.message,
        "page_context": page_ctx,
        "history": history,
        "errors": [],
        "should_compact": False,
        "tenant_id": conv.get("tenantId") or "00000000-0000-0000-0000-000000000001",
    }

    agent_name = conv["agentName"]
    pg = _get_pg_executor(agent_name)

    graph = _get_or_build_graph(app, agent_name)
    pubsub = await sse_streamer.open_channel(session_id)

    conv_rows = await pg.run("fetch_conversation", conversation_id, user_id)
    conv_row = conv_rows[0] if conv_rows else None

    defn = load_agent_definition(agent_name)

    task = asyncio.create_task(
        run_conversation_turn(
            state=state,
            graph=graph,
            session_store=session_store,
            sse_streamer=sse_streamer,
            session_meta=meta,
            pg_executor=pg,
            conversation_id=conversation_id,
            conversation_row=conv_row,
            ceiling_warning_pct=defn.conversation.ceiling_warning_pct,
            ceiling_critical_pct=defn.conversation.ceiling_critical_pct,
        )
    )
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)

    return StreamingResponse(
        sse_streamer.stream_channel(pubsub, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/conversations")
async def list_conversations_endpoint(
    user_id: str = Depends(_require_user_id),
    tenant_id: str | None = Depends(_extract_tenant_id),
    status: str | None = None,
    contextType: str | None = None,
    contextEntityId: str | None = None,
    contextLabel: str | None = None,
    offset: int = 0,
    limit: int = 20,
):
    """List conversations for the authenticated user."""
    if limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1-100")
    pg = _get_pg_executor()
    tid = tenant_id or "default"

    items, total = await list_conversations(
        pg,
        user_id=user_id,
        tenant_id=tid,
        status=status,
        context_type=contextType,
        context_entity_id=contextEntityId,
        context_label=contextLabel,
        limit=limit,
        offset=offset,
    )
    return {"data": items, "pagination": {"total": total, "offset": offset, "limit": limit}}


@app.get("/api/v1/conversations/{conversation_id}")
async def get_conversation_endpoint(
    conversation_id: str,
    user_id: str = Depends(_require_user_id),
):
    """Get conversation detail."""
    pg = _get_pg_executor()
    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@app.get("/api/v1/conversations/{conversation_id}/messages")
async def get_conversation_messages_endpoint(
    conversation_id: str,
    user_id: str = Depends(_require_user_id),
    offset: int = 0,
    limit: int = 50,
):
    """Get full message history for a conversation."""
    if limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1-200")
    pg = _get_pg_executor()

    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages, total = await get_messages(pg, conversation_id, limit=limit, offset=offset)
    return {"data": messages, "pagination": {"total": total, "offset": offset, "limit": limit}}


@app.patch("/api/v1/conversations/{conversation_id}")
async def patch_conversation_endpoint(
    conversation_id: str,
    ops: list[JsonPatchOp],
    user_id: str = Depends(_require_user_id),
):
    """JSON Patch (RFC 6902) — update conversation title or status."""
    pg = _get_pg_executor()

    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not ops:
        raise HTTPException(status_code=400, detail="Patch array must contain at least one operation")
    if len(ops) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 patch operations per request")

    _PATCHABLE = {"/title", "/status"}
    _VALID_TRANSITIONS = {
        "ACTIVE": {"CLOSED"},
        "CEILING_HIT": {"CLOSED", "ACTIVE"},
        "CLOSED": {"ARCHIVED", "ACTIVE"},
    }

    for op in ops:
        if op.path not in _PATCHABLE:
            raise HTTPException(status_code=403, detail=f"Path {op.path} is not patchable")

        if op.op == "test":
            current = conv.get(op.path.lstrip("/"))
            if current != op.value:
                raise HTTPException(
                    status_code=409,
                    detail=f"Test failed: {op.path} is {current!r}, expected {op.value!r}",
                )
            continue

        if op.op == "replace":
            if op.path == "/title":
                if not isinstance(op.value, str) or len(op.value) > 500:
                    raise HTTPException(status_code=422, detail="title must be a string (max 500 chars)")
                await pg.execute("update_conversation_title", conversation_id, op.value, user_id)

            elif op.path == "/status":
                current_status = conv["status"]
                allowed = _VALID_TRANSITIONS.get(current_status, set())
                if op.value not in allowed:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot transition from {current_status} to {op.value}",
                    )
                await pg.execute("update_conversation_status", conversation_id, op.value, user_id)

    updated = await get_conversation(pg, conversation_id, user_id)
    return updated


@app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
async def delete_conversation_endpoint(
    conversation_id: str,
    user_id: str = Depends(_require_user_id),
):
    """Archive a conversation (soft-delete)."""
    pg = _get_pg_executor()
    conv = await get_conversation(pg, conversation_id, user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv["status"] not in ("CLOSED",):
        raise HTTPException(status_code=409, detail="Close the conversation before archiving")
    await pg.execute("update_conversation_status", conversation_id, "ARCHIVED", user_id)


# ── Multi-Agent Chat API (/api/v1/chat) ──────────────────────────────────────


@app.post("/api/v1/chat")
async def supervisor_chat_endpoint(
    request: ConversationChatRequest,
    user_id: str = Depends(_require_user_id),
    tenant_id: str | None = Depends(_extract_tenant_id),
    x_auth_user_name: str | None = Header(default=None),
    x_auth_user_roles: str | None = Header(default=None),
    x_auth_is_super_admin: str | None = Header(default=None),
    x_source_channel: str | None = Header(default=None),
) -> StreamingResponse:
    """Multi-agent chat with structured responses.

    The supervisor classifies intent, delegates to specialist agents,
    and returns structured response blocks via SSE.
    """
    import json as _json
    import uuid as _uuid

    from smi_agent.agents.audit import AuditLogger
    from smi_agent.agents.guardrails import InputGuardrails, OutputGuardrails
    from smi_agent.config.redis_keys import user_context_key
    from smi_agent.streaming import StepEmitter

    valid, reason = InputGuardrails.validate(request.message)
    if not valid:
        raise HTTPException(status_code=422, detail=reason)

    supervisor = app.state.supervisor
    sse_streamer: SSEStreamer = app.state.sse_streamer
    redis_client = sse_streamer._r

    stream_id = _uuid.uuid4().hex
    tid = tenant_id or "00000000-0000-0000-0000-000000000001"

    _user_key = user_context_key(user_id)
    _USER_CTX_TTL = 3600
    created = await redis_client.set(
        _user_key,
        _json.dumps({
            "user_id": user_id,
            "user_name": x_auth_user_name or "unknown",
            "tenant_id": tid,
            "roles": x_auth_user_roles or "",
            "is_super_admin": x_auth_is_super_admin == "true",
            "source_channel": x_source_channel or "WEB",
        }),
        ex=_USER_CTX_TTL,
        nx=True,
    )
    if not created:
        await redis_client.expire(_user_key, _USER_CTX_TTL)

    context = {
        "user_id": user_id,
        "tenant_id": tid,
        "entity_id": getattr(request, "pageContext", None) and request.pageContext.entity_id or "",
    }

    pubsub = await sse_streamer.open_channel(stream_id)

    async def _run_supervisor():
        try:
            step_emitter = StepEmitter(sse_streamer._r, stream_id)

            response = await supervisor.handle(
                user_message=request.message,
                context=context,
                step_emitter=step_emitter,
            )

            response = OutputGuardrails.sanitize(response)

            await sse_streamer.publish_response(
                stream_id, response.model_dump(mode="json", by_alias=True)
            )

            try:
                record = AuditLogger.from_response(response, request.message)
                pg_exec = _get_pg_executor() if app.state.pg_client else None
                AuditLogger.log_fire_and_forget(pg_exec, record)
            except Exception:
                pass

        except BaseException:
            logger.exception("Supervisor chat failed for stream %s", stream_id)
            import contextlib
            with contextlib.suppress(Exception):
                await sse_streamer.publish_error(stream_id, "An error occurred. Please try again.")
        finally:
            await sse_streamer.publish_done(stream_id)

    task = asyncio.create_task(_run_supervisor())
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)

    return StreamingResponse(
        sse_streamer.stream_channel(pubsub, stream_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Investigation API (/api/ws/investigations) ────────────────────────────────


@app.post("/api/ws/investigations/stream")
async def create_investigation_stream(
    request: CreateInvestigationRequest,
    _: None = Depends(_require_api_key),
    auth_tenant_id: str | None = Depends(_extract_tenant_id),
) -> StreamingResponse:
    """Create an investigation and stream reasoning steps via SSE."""
    if auth_tenant_id and auth_tenant_id != request.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="tenant_id in request body does not match X-Auth-Tenant-Id header",
        )

    sse_streamer: SSEStreamer = app.state.sse_streamer
    pg_client = app.state.pg_client

    if pg_client is None:
        raise HTTPException(status_code=503, detail="Investigation creation unavailable: Postgres not configured")

    import uuid as _uuid

    stream_id = _uuid.uuid4().hex

    pubsub = await sse_streamer.open_channel(stream_id)

    task = asyncio.create_task(
        run_investigation_creation(
            request=request,
            pg_client=pg_client,
            driver=app.state.driver,
            template_loader=app.state.template_loader,
            sse_streamer=sse_streamer,
            stream_id=stream_id,
        )
    )
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)

    return StreamingResponse(
        sse_streamer.stream_channel(pubsub, stream_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/investigations/{investigation_id}/stream")
async def investigation_stream(
    investigation_id: str,
    _: None = Depends(_require_api_key),
) -> StreamingResponse:
    """SSE stream of reasoning steps for an investigation."""
    sse_streamer: SSEStreamer = app.state.sse_streamer
    pubsub = await sse_streamer.open_channel(investigation_id)
    return StreamingResponse(
        sse_streamer.stream_channel(pubsub, investigation_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )