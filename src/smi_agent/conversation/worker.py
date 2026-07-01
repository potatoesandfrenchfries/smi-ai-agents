"""Conversation queue worker — competing-consumer loop."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from smi_agent.conversation.queue import QueueConsumer, StompConnectionError

if TYPE_CHECKING:
    from smi_agent.conversation.session_store import SessionStore
    from smi_agent.conversation.sse import SSEStreamer

logger = logging.getLogger(__name__)


async def run_conversation_worker(
    consumer: QueueConsumer,
    graph: Any,
    session_store: SessionStore,
    sse_streamer: SSEStreamer,
) -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from smi_agent.api.conversation_runner import run_conversation_turn  # noqa: PLC0415
    from smi_agent.conversation.schemas import SessionMeta  # noqa: PLC0415

    try:
        async for message in consumer.consume():
            msg_id = message.pop("_id", "")
            try:
                session_id = message.get("session_id", "")
                agent_name = message.get("agent_name", "uc02_conversation")
                meta = SessionMeta(session_id=session_id, agent_name=agent_name)
                await run_conversation_turn(
                    state=message,
                    graph=graph,
                    session_store=session_store,
                    sse_streamer=sse_streamer,
                    session_meta=meta,
                )
                await consumer.ack(msg_id)
            except (ValueError, ValidationError) as exc:
                logger.warning("conversation_worker: nack message %s: %s", msg_id, exc)
                await consumer.nack(msg_id)
            except (SystemExit, KeyboardInterrupt):
                break
            except Exception as exc:
                logger.error("conversation_worker: unexpected error for %s: %s", msg_id, exc)
                await consumer.nack(msg_id)
    except StompConnectionError:
        logger.error("conversation_worker: STOMP connection lost — worker will exit")
        raise
