"""Entry point for the conversation worker process."""

from __future__ import annotations

import asyncio
import os

import uvicorn

from smi_agent.api.app import app


def main() -> None:
    """smi-conversation-worker entry point."""
    asyncio.run(_run())


async def _run() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
    server = uvicorn.Server(config)
    await server.serve()
