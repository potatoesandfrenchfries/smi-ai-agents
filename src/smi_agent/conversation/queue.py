"""ActiveMQ/STOMP competing-consumer queue client (stomp-py)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class StompConnectionError(RuntimeError):
    """Raised when the STOMP connection drops unexpectedly."""


@runtime_checkable
class QueueConsumer(Protocol):
    async def consume(self) -> AsyncIterator[dict[str, Any]]: ...
    async def ack(self, message_id: str) -> None: ...
    async def nack(self, message_id: str) -> None: ...


class StompQueueConsumer:
    """
    Async STOMP consumer built on stomp-py.

    stomp-py is synchronous/callback-based. We bridge it to asyncio by running
    the blocking connect in a thread and having the listener post frames to an
    asyncio.Queue via loop.call_soon_threadsafe.
    """

    def __init__(
        self,
        host: str,
        port: int,
        destination: str,
        username: str,
        password: str,
    ) -> None:
        self._host = host
        self._port = port
        self._destination = destination
        self._username = username
        self._password = password
        self._conn: Any = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    def _build_listener(self, loop: asyncio.AbstractEventLoop) -> Any:
        """Return a stomp ConnectionListener that forwards frames to our asyncio queue."""
        try:
            import stomp  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "stomp-py package is required for ActiveMQ integration. "
                "Install with: uv add stomp-py"
            ) from exc

        queue = self._queue

        class _Listener(stomp.ConnectionListener):
            def on_message(self, frame: Any) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, frame)

            def on_error(self, frame: Any) -> None:
                body = getattr(frame, "body", "") or ""
                exc = StompConnectionError(f"STOMP error frame: {body}")
                loop.call_soon_threadsafe(queue.put_nowait, exc)

            def on_disconnected(self) -> None:
                exc = StompConnectionError("STOMP server disconnected unexpectedly")
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        return _Listener()

    def _connect_sync(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking connect — intended to run in a thread via asyncio.to_thread."""
        try:
            import stomp  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "stomp-py package is required for ActiveMQ integration. "
                "Install with: uv add stomp-py"
            ) from exc

        self._conn = stomp.Connection([(self._host, self._port)])
        self._conn.set_listener("smi", self._build_listener(loop))
        self._conn.connect(self._username, self._password, wait=True)
        self._conn.subscribe(
            destination=self._destination,
            id=1,
            ack="client-individual",
        )
        logger.info(
            "STOMP connected to %s:%d, subscribed to %s",
            self._host,
            self._port,
            self._destination,
        )

    async def _connect(self) -> None:
        loop = asyncio.get_event_loop()
        await asyncio.to_thread(self._connect_sync, loop)

    async def consume(self) -> AsyncIterator[dict[str, Any]]:
        if self._conn is None or not self._conn.is_connected():
            await self._connect()
        while True:
            item = await self._queue.get()
            if isinstance(item, StompConnectionError):
                raise item
            if isinstance(item, Exception):
                raise StompConnectionError(f"STOMP error: {item}") from item
            # item is a stomp Frame object
            try:
                body = json.loads(item.body)
                body["_id"] = item.headers.get("message-id", "")
                yield body
            except json.JSONDecodeError:
                logger.warning("queue: received non-JSON frame, skipping")

    async def ack(self, message_id: str) -> None:
        if self._conn and self._conn.is_connected():
            await asyncio.to_thread(self._conn.ack, message_id)

    async def nack(self, message_id: str) -> None:
        if self._conn and self._conn.is_connected():
            await asyncio.to_thread(self._conn.nack, message_id)


def make_stomp_consumer(
    url: str,
    destination: str,
    credentials: tuple[str, str],
) -> StompQueueConsumer:
    """Create a StompQueueConsumer from a URL string like 'stomp://host:61613'."""
    url = url.removeprefix("tcp://").removeprefix("stomp://").removeprefix("stomp+ssl://")
    host, _, port_str = url.partition(":")
    port = int(port_str) if port_str else 61613
    return StompQueueConsumer(
        host=host,
        port=port,
        destination=destination,
        username=credentials[0],
        password=credentials[1],
    )
