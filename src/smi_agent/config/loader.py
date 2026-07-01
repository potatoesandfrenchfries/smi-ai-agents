"""Loads and validates an AgentDefinition YAML by name.

Usage::

    from smi_agent.config import load_agent_definition

    defn = load_agent_definition("uc02_investigation_plan")
    print(defn.hash)          # sha256:abc...
    print(defn.budget.max_cost_usd)

Process-level caching
---------------------
Results are cached by ``(resolved_path, mtime_ns)``.  Repeated calls within
the same worker process (e.g. successive Temporal activity invocations using
the same config name) pay only a single ``stat`` syscall after the first load.
The cache invalidates automatically when the YAML file is edited in place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import typing
from pathlib import Path

import yaml

from smi_agent.config.models import AgentDefinition

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default definitions directory
# ---------------------------------------------------------------------------
# Honour SMI_DEFINITIONS_DIR first so container deployments can mount the
# agent_definitions/ volume at an arbitrary path without code changes.
# The __file__-relative fallback is correct for editable installs and local dev;
# it breaks inside a wheel where the package lands in site-packages — hence the env var.
_DEFAULT_DEFINITIONS_DIR = Path(
    os.environ.get(
        "SMI_DEFINITIONS_DIR",
        str(Path(__file__).parents[3] / "agent_definitions"),
    )
)

# ---------------------------------------------------------------------------
# Process-level cache: (resolved_path_str, mtime_ns) → AgentDefinition
# ---------------------------------------------------------------------------
_DEFINITION_CACHE: dict[tuple[str, int], AgentDefinition] = {}


def load_agent_definition(
    name: str,
    definitions_dir: Path | str | None = None,
) -> AgentDefinition:
    """Load and validate an AgentDefinition YAML by config name.

    Args:
        name: Bare config name (e.g. ``"uc02_investigation_plan"``). Must
            match a file ``<name>.yaml`` in *definitions_dir*. Must not
            contain path separators or ``..`` components.
        definitions_dir: Directory containing YAML configs. Defaults to
            ``$SMI_DEFINITIONS_DIR`` env var, then ``agent_definitions/``
            relative to the project root.

    Returns:
        Validated :class:`~smi_agent.config.models.AgentDefinition` with
        ``.hash`` set to ``sha256:<hex>`` of the raw file bytes.

    Raises:
        ValueError: ``name`` contains unsafe path components.
        FileNotFoundError: YAML file does not exist.
        ValueError: File is not a valid YAML mapping.
        pydantic.ValidationError: File fails schema validation.
    """
    dir_path = Path(definitions_dir) if definitions_dir is not None else _DEFAULT_DEFINITIONS_DIR

    # ── Security: path traversal guard ────────────────────────────────────────
    # Resolve first so that ``..`` and symlinks are fully normalised before the
    # containment check.  Append .yaml *after* resolving the directory so that
    # the final component (which doesn't exist yet) doesn't cause resolve() to
    # fail on missing-path OSes.
    resolved_dir = dir_path.resolve()
    yaml_path = (dir_path / f"{name}.yaml").resolve()
    try:
        yaml_path.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            f"Invalid agent definition name: {name!r}. "
            "Name must not contain path separators or '..' components."
        ) from exc

    # ── Stat: replaces separate exists() + read_bytes() (removes TOCTOU) ─────
    try:
        stat = yaml_path.stat()
    except FileNotFoundError:
        # Log helpful path/listing detail at DEBUG only — not in the exception
        # message, which may surface to callers in Temporal failure details or
        # HTTP error bodies.
        available = sorted(p.stem for p in dir_path.glob("*.yaml")) if dir_path.exists() else []
        logger.debug(
            "Agent definition '%s' not found at %s. Available: %s",
            name,
            yaml_path,
            available or "(none)",
        )
        raise FileNotFoundError(f"Agent definition '{name}' not found.") from None

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cache_key = (str(yaml_path), stat.st_mtime_ns)
    if cache_key in _DEFINITION_CACHE:
        return _DEFINITION_CACHE[cache_key]

    # ── Load and hash ─────────────────────────────────────────────────────────
    raw_bytes = yaml_path.read_bytes()
    config_hash = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"

    data = yaml.safe_load(raw_bytes)
    if not isinstance(data, dict):
        raise ValueError(
            f"Agent definition '{name}' must be a YAML mapping, got {type(data).__name__}"
        )

    # Strip any ``hash`` key from YAML data — hash is injected by the loader
    # only, never authored in the file.  This prevents YAML authors from
    # spoofing a config hash.
    data.pop("hash", None)

    # ── Unknown-field warnings (recursive) ────────────────────────────────────
    _warn_unknown_fields(data, AgentDefinition, name)

    # ── Validate and inject hash in one shot (no post-construction mutation) ──
    definition = AgentDefinition.model_validate({**data, "hash": config_hash})

    logger.debug(
        "Loaded agent definition '%s' version=%s hash=%s",
        definition.name,
        definition.version,
        config_hash,
    )

    _DEFINITION_CACHE[cache_key] = definition
    return definition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _warn_unknown_fields(
    data: dict,
    model_cls: type,
    config_name: str,
    path: str = "",
) -> None:
    """Recursively warn on unknown fields at every nesting level.

    Unknown fields are logged as warnings rather than raising so that a YAML
    written for a future version of smi_agent still loads correctly on an
    older worker (forward-compatibility).
    """
    known_fields = frozenset(model_cls.model_fields)
    unknown = set(data) - known_fields - {"hash"}  # hash is loader-injected, not a typo
    for key in unknown:
        field_path = f"{path}.{key}" if path else key
        logger.warning(
            "Agent definition '%s': unknown field '%s' ignored. "
            "Check for a typo or upgrade smi_agent to support this field.",
            config_name,
            field_path,
        )
    for key in set(data) & known_fields:
        if isinstance(data[key], dict):
            inner_cls = _unwrap_model_type(model_cls.model_fields[key].annotation)
            if inner_cls is not None:
                child_path = f"{path}.{key}" if path else key
                _warn_unknown_fields(data[key], inner_cls, config_name, child_path)


def _unwrap_model_type(annotation: type | None) -> type | None:
    """Extract a Pydantic model class from ``Model``, ``Model | None``, etc.

    Returns the model class if found, ``None`` otherwise.
    """
    if annotation is None:
        return None
    if hasattr(annotation, "model_fields"):
        return annotation
    # Handle ``Optional[X]`` / ``X | None`` / other ``Union`` forms
    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        for arg in typing.get_args(annotation):
            if hasattr(arg, "model_fields"):
                return arg
    return None
