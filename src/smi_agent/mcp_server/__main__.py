"""Entry point for the `smi-mcp-server` process (see pyproject.toml
[project.scripts]) — mirrors worker.py's main() shape.

Env vars (default shown):
    SMI_MCP_SERVER_HOST   0.0.0.0
    SMI_MCP_SERVER_PORT   9200
"""

from __future__ import annotations

import logging
import os


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    from smi_agent.mcp_server.server import mcp

    host = os.environ.get("SMI_MCP_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("SMI_MCP_SERVER_PORT", "9200"))
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
