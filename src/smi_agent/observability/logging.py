"""structlog JSON configuration with contextvars for investigation tracing.

Call ``configure_logging()`` once at worker/process startup.  After that, any
module can bind per-request context via::

    from smi_agent.observability.logging import bind_investigation_context, clear_context

    bind_investigation_context(
        investigation_id="inv-001",
        exposure_id="EXP-1621",
        node_name="plan_generation",
    )
    log = structlog.get_logger()
    log.info("plan_generation started")
    # JSON output: {"event": "plan_generation started", "investigation_id": "inv-001", ...}

The context is stored in ``contextvars`` so it is safe for async tasks and
concurrent investigations running in the same process.

Rolling file handler:
    - Max 10 MB per file, 5 backup files (50 MB total)
    - Buffered writes (8 KB buffer) for optimized I/O
    - JSON format to file, console renderer for dev
    - Configurable via env vars: SMI_LOG_DIR, SMI_LOG_MAX_BYTES, SMI_LOG_BACKUP_COUNT
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    reset_contextvars,
)

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_BACKUP_COUNT = 5  # 5 rotated files (50 MB total)
_DEFAULT_BUFFER_SIZE = 8 * 1024  # 8 KB write buffer

# Dedicated logger name for tracing the dynamic (Supervisor/Planner) routing
# path: intent classification -> supervisor routing -> planner tool selection
# -> specialist delegation -> provider search. Kept separate from the app's
# main JSON logger so the path can be watched live, in plain text, with:
#   tail -f logs/planner_trace.log
_PLANNER_TRACE_LOGGER_NAME = "smi_agent.planner_trace"


def configure_logging(
    log_level: str | None = None,
    json_output: bool | None = None,
) -> None:
    """Configure structlog + stdlib logging for the worker process.

    Args:
        log_level: Override; defaults to ``SMI_LOG_LEVEL`` env var or ``INFO``.
        json_output: If True emit JSON. Defaults to True unless ``SMI_LOG_JSON=false``.

    Environment variables:
        SMI_LOG_LEVEL:        Log level (default: INFO)
        SMI_LOG_JSON:         JSON output (default: true)
        SMI_LOG_DIR:          Directory for log files (default: logs/)
        SMI_LOG_FILE:         Log filename (default: smi-agent.log)
        SMI_LOG_MAX_BYTES:    Max bytes per file before rotation (default: 10485760 = 10MB)
        SMI_LOG_BACKUP_COUNT: Number of rotated backup files (default: 5)
        SMI_LOG_TO_FILE:      Enable file logging (default: true)
    """
    level_str = log_level or os.environ.get("SMI_LOG_LEVEL", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)

    use_json = json_output
    if use_json is None:
        use_json = os.environ.get("SMI_LOG_JSON", "true").lower() not in ("false", "0", "no")

    # ── structlog processors ──────────────────────────────────────────────────
    processors = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if use_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # ── stdlib root logger ────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)
    # Clear any existing handlers to avoid duplicates on re-configure
    root.handlers.clear()

    # Console handler (always)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_make_formatter(use_json))
    root.addHandler(console)

    # Rolling file handler (opt-in via env, default enabled)
    log_to_file = os.environ.get("SMI_LOG_TO_FILE", "true").lower() not in ("false", "0", "no")
    if log_to_file:
        file_handler = _make_rolling_file_handler(level, use_json=True)
        if file_handler:
            root.addHandler(file_handler)

    # Quieten noisy libraries
    for noisy in ("httpx", "httpcore", "neo4j", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Dedicated plain-text trace log for the dynamic routing path. Still
    # propagates to the root logger (so it also appears in the normal
    # console/JSON log), but gets its own human-readable file regardless of
    # SMI_LOG_JSON, so it can be tailed on its own during debugging.
    trace_logger = logging.getLogger(_PLANNER_TRACE_LOGGER_NAME)
    trace_logger.setLevel(level)
    trace_logger.propagate = True
    for existing in list(trace_logger.handlers):
        trace_logger.removeHandler(existing)
    trace_handler = _make_planner_trace_handler(level)
    if trace_handler:
        trace_logger.addHandler(trace_handler)


def _make_formatter(use_json: bool) -> logging.Formatter:
    """Create a log formatter."""
    if use_json:
        return logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        )
    return logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _make_rolling_file_handler(
    level: int,
    *,
    use_json: bool = True,
) -> logging.Handler | None:
    """Create a buffered rotating file handler.

    Returns None if the log directory cannot be created.

    File rotation:
        - maxBytes:    10 MB per file (configurable via SMI_LOG_MAX_BYTES)
        - backupCount: 5 files kept (configurable via SMI_LOG_BACKUP_COUNT)
        - Total disk:  ~60 MB max (current + 5 backups)

    Buffered I/O:
        - Uses MemoryHandler with 8 KB buffer capacity
        - Flushes on WARNING+ or when buffer is full
        - Reduces disk I/O for high-throughput DEBUG/INFO logging
    """
    log_dir = os.environ.get("SMI_LOG_DIR", "logs")
    log_file = os.environ.get("SMI_LOG_FILE", "smi-agent.log")
    max_bytes = int(os.environ.get("SMI_LOG_MAX_BYTES", str(_DEFAULT_MAX_BYTES)))
    backup_count = int(os.environ.get("SMI_LOG_BACKUP_COUNT", str(_DEFAULT_BACKUP_COUNT)))

    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        filepath = log_path / log_file
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Cannot create log directory %s: %s — file logging disabled",
            log_dir,
            exc,
        )
        return None

    # Rotating file handler — always JSON for machine parsing
    rotating = RotatingFileHandler(
        filename=str(filepath),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    rotating.setLevel(level)
    rotating.setFormatter(_make_formatter(use_json=True))

    # Wrap in MemoryHandler for buffered writes (reduces I/O syscalls)
    buffered = logging.handlers.MemoryHandler(
        capacity=_DEFAULT_BUFFER_SIZE,
        flushLevel=logging.WARNING,  # flush immediately on WARNING+
        target=rotating,
        flushOnClose=True,
    )
    buffered.setLevel(level)

    return buffered


def _make_planner_trace_handler(level: int) -> logging.Handler | None:
    """Rotating, always-plain-text handler for logs/planner_trace.log.

    Returns None if the log directory cannot be created (mirrors
    ``_make_rolling_file_handler``'s failure mode).
    """
    log_dir = os.environ.get("SMI_LOG_DIR", "logs")
    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        filepath = log_path / "planner_trace.log"
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Cannot create planner trace log dir %s: %s — planner_trace.log disabled",
            log_dir, exc,
        )
        return None

    handler = RotatingFileHandler(
        filename=str(filepath),
        maxBytes=_DEFAULT_MAX_BYTES,
        backupCount=_DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%H:%M:%S")
    )
    return handler


def get_planner_trace_logger() -> logging.Logger:
    """Logger for the dynamic Supervisor/Planner routing path.

    Every intent classification, routing choice, and specialist tool-call
    made while handling a request through the dynamic (non-Temporal) chat
    stack should log here, so the full decision path can be watched live in
    one place: ``tail -f logs/planner_trace.log``.
    """
    return logging.getLogger(_PLANNER_TRACE_LOGGER_NAME)


# ── Context helpers ────────────────────────────────────────────────────────────


def bind_investigation_context(
    investigation_id: str | None = None,
    entity_id: str | None = None,
    node_name: str | None = None,
    **extra: object,
) -> None:
    """Bind structured log context for the current async task.

    All subsequent log calls in this coroutine (and its children) will include
    these fields automatically.
    """
    ctx: dict[str, object] = {}
    if investigation_id is not None:
        ctx["investigation_id"] = investigation_id
    if entity_id is not None:
        ctx["entity_id"] = entity_id
    if node_name is not None:
        ctx["node_name"] = node_name
    ctx.update(extra)
    if ctx:
        bind_contextvars(**ctx)


def clear_context() -> None:
    """Clear all bound context variables for the current task."""
    clear_contextvars()


def reset_context(token: object) -> None:
    """Reset context to a previous state (for scoped binding)."""
    reset_contextvars(token)  # type: ignore[arg-type]
