# syntax=docker/dockerfile:1

# ── Builder ───────────────────────────────────────────────────────────────────
# Installs from uv.lock via `uv sync --frozen`, so the image gets the exact
# dependency set resolved and verified locally — not whatever `pip install`
# happens to resolve at build time (which is how version drift like
# transitively-broken sub-dependencies sneaks into a prod image unnoticed).
FROM ghcr.io/astral-sh/uv:latest AS uv
FROM python:3.12-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /build

# Lockfile + dependency metadata first, so `uv sync` is cached across
# source-only changes and never re-resolves versions inside the build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-editable --extra conversation

# Now the project itself. --no-editable is required here: uv sync installs
# the root project editable by default (a .pth pointing back at /build),
# which breaks at runtime since only /opt/venv is copied forward, not /build.
COPY src ./src
RUN uv sync --frozen --no-editable --extra conversation

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # config/loader.py and llm/prompts.py both locate their directories via
    # `Path(__file__).parents[N]`, which resolves to the repo root only when
    # running from source (PYTHONPATH=src, local dev). Installed into
    # site-packages as this image does, that heuristic instead lands
    # somewhere under /opt/venv — both modules document exactly this env var
    # as the override for a containerised/wheel deployment; without it,
    # every agent definition and prompt template fails to load at runtime.
    SMI_DEFINITIONS_DIR=/app/agent_definitions \
    SMI_PROMPTS_ROOT=/app \
    # neo4j_client/templates.py has the identical parents[N]-relative bug,
    # pointed at a `cypher/` dir that must merely exist (it may be empty —
    # TemplateLoader only warns on zero templates, it doesn't raise). Without
    # this, TemplateLoader() raises during lifespan startup and the
    # conversation API never binds a port, in every environment, not just
    # Docker — the directory didn't exist anywhere in the repo before.
    SMI_CYPHER_DIR=/app/cypher

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Runtime assets loaded at execution time (prompt templates, agent definitions,
# Cypher query templates).
COPY --chown=appuser:appuser agent_definitions ./agent_definitions
COPY --chown=appuser:appuser prompts ./prompts
COPY --chown=appuser:appuser cypher ./cypher
COPY --chown=appuser:appuser src ./src

USER appuser

# Conversation API port (see Makefile `api` target).
EXPOSE 8080

# Default to the conversation API. Override the command to run the Temporal
# worker instead:  docker run <image> smi-agent-worker
CMD ["smi-conversation-worker"]
