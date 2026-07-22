# syntax=docker/dockerfile:1

# ── Builder ───────────────────────────────────────────────────────────────────
# Build a wheel and install into an isolated virtualenv so the final image
# carries only runtime artifacts, not build tooling.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Isolated venv we copy verbatim into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the build backend needs first, so dependency resolution is
# cached across source-only changes.
COPY pyproject.toml ./
COPY src ./src

# Install the package plus the conversation extras (FastAPI/uvicorn) required to
# serve the API. Drop the "-e" so the venv is self-contained.
RUN pip install ".[conversation]"

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Runtime assets loaded at execution time (prompt templates, agent definitions).
COPY --chown=appuser:appuser agent_definitions ./agent_definitions
COPY --chown=appuser:appuser prompts ./prompts
COPY --chown=appuser:appuser src ./src

USER appuser

# Conversation API port (see Makefile `api` target).
EXPOSE 8080

# Default to the conversation API. Override the command to run the Temporal
# worker instead:  docker run <image> smi-agent-worker
CMD ["smi-conversation-worker"]
