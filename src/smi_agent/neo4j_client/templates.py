"""Load and validate Cypher templates from paired .cypher + .meta.yaml files.

Each template is a pair::

    cypher/{name}.cypher      — parameterized Cypher query
    cypher/{name}.meta.yaml   — parameter specs, graph_touches, cost_class, etc.

``TemplateLoader`` validates every template at load time so the worker fails
fast on misconfiguration rather than at request time.

``CypherTemplate.render(params)`` returns the final query string with
*interpolated* parameters (e.g. hop-bound integers) substituted in, and the
remaining Cypher-parameter dict ready to pass to the driver.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

_DEFAULT_CYPHER_DIR = Path(
    os.environ.get(
        "SMI_CYPHER_DIR",
        str(Path(__file__).parents[3] / "cypher"),
    )
)

# Timeout (seconds) keyed by cost_class
COST_CLASS_TIMEOUTS: dict[str, float] = {
    "cheap": 1.0,
    "medium": 5.0,
    "expensive": 15.0,
}


# ── Meta-yaml Pydantic models ──────────────────────────────────────────────────


class ClampSpec(BaseModel):
    min: int | float | None = None
    max: int | float | None = None


class ParameterSpec(BaseModel):
    type: Literal["str", "int", "float", "list[str]"]
    required: bool = True
    # interpolate=True: value must be string-substituted into the query text
    # (not passed as a Cypher parameter) because Neo4j does not allow parameters
    # in variable-length relationship bounds (*0..$n).
    interpolate: bool = False
    default: Any = None
    clamp: ClampSpec | None = None

    @model_validator(mode="after")
    def _interpolated_must_be_int(self) -> ParameterSpec:
        if self.interpolate and self.type != "int":
            raise ValueError(
                "Only 'int' parameters may use interpolate=true "
                "(variable-length bound substitution requires an integer)."
            )
        return self


class GraphTouches(BaseModel):
    labels: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)


class TemplateMeta(BaseModel):
    """Validated contents of a .meta.yaml file."""

    description: str = ""
    cost_class: Literal["cheap", "medium", "expensive"] = "medium"
    allowed_agents: list[str] = Field(default_factory=lambda: ["*"])
    graph_touches: GraphTouches = Field(default_factory=GraphTouches)
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)


# ── Resolved template (meta + query text) ─────────────────────────────────────


class CypherTemplate:
    """A loaded, validated Cypher template ready for execution."""

    __slots__ = (
        "name",
        "cypher_text",
        "description",
        "cost_class",
        "timeout",
        "allowed_agents",
        "parameters",
        "graph_touches",
    )

    def __init__(self, name: str, cypher_text: str, meta: TemplateMeta) -> None:
        self.name = name
        self.cypher_text = cypher_text
        self.description = meta.description
        self.cost_class = meta.cost_class
        self.timeout = COST_CLASS_TIMEOUTS[meta.cost_class]
        self.allowed_agents = meta.allowed_agents
        self.parameters = meta.parameters
        self.graph_touches = meta.graph_touches

    def render(self, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Apply parameter clamping and return (query_text, cypher_params).

        Interpolated parameters (``interpolate=True``) are substituted into
        the query text as validated integers and removed from the Cypher
        parameter dict.  All other parameters are passed through as Cypher
        parameters after clamping.

        Returns:
            A tuple of ``(rendered_cypher_text, cypher_parameter_dict)``.

        Raises:
            TemplateParameterError: A required parameter is missing or a value
                fails the integer guard for an interpolated parameter.
        """
        resolved = self._resolve_defaults(params)
        self._check_required(resolved)
        clamped = self._clamp(resolved)

        query = self.cypher_text
        cypher_params: dict[str, Any] = {}

        for param_name, spec in self.parameters.items():
            value = clamped.get(param_name)
            if value is None:
                continue
            if spec.interpolate:
                # Integer guard: ensure value is an int before substituting
                if not isinstance(value, int):
                    raise TemplateParameterError(
                        self.name,
                        param_name,
                        f"interpolated parameter must be an int, got {type(value).__name__}",
                    )
                query = query.replace(f"${param_name}", str(value))
            else:
                cypher_params[param_name] = value

        return query, cypher_params

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _resolve_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(params)
        for name, spec in self.parameters.items():
            if name not in resolved and spec.default is not None:
                resolved[name] = spec.default
        return resolved

    def _check_required(self, params: dict[str, Any]) -> None:
        missing = [
            name for name, spec in self.parameters.items() if spec.required and name not in params
        ]
        if missing:
            raise TemplateParameterError(
                self.name,
                missing[0],
                f"required parameter(s) missing: {missing!r}",
            )

    def _clamp(self, params: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name, value in params.items():
            spec = self.parameters.get(name)
            if spec is None or spec.clamp is None:
                result[name] = value
                continue
            c = spec.clamp
            if c.min is not None and value < c.min:
                logger.debug(
                    "Template '%s': clamping %s from %s to min %s",
                    self.name,
                    name,
                    value,
                    c.min,
                )
                value = c.min
            if c.max is not None and value > c.max:
                logger.debug(
                    "Template '%s': clamping %s from %s to max %s",
                    self.name,
                    name,
                    value,
                    c.max,
                )
                value = c.max
            result[name] = value
        return result


# ── Errors ─────────────────────────────────────────────────────────────────────


class TemplateNotFoundError(KeyError):
    """Raised when a template name is not in the loaded registry."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.template_name = name
        self.available = available
        super().__init__(f"Cypher template '{name}' not found")


class TemplateParameterError(ValueError):
    """Raised on missing required parameters or interpolation guard failures."""

    def __init__(self, template_name: str, param_name: str, reason: str) -> None:
        self.template_name = template_name
        self.param_name = param_name
        super().__init__(f"[{template_name}] parameter '{param_name}': {reason}")


class TemplateLoadError(ValueError):
    """Raised when a template file pair is malformed."""


# ── Template loader ────────────────────────────────────────────────────────────


class TemplateLoader:
    """Loads all .cypher + .meta.yaml pairs from a directory.

    Validates every template at construction time.  Raises ``TemplateLoadError``
    immediately if any file is missing or malformed so the worker fails fast.

    Usage::

        loader = TemplateLoader()                       # default cypher/ dir
        loader = TemplateLoader(Path("cypher/"))
        template = loader["fetch_exposure_context"]
    """

    def __init__(self, cypher_dir: Path | None = None) -> None:
        self._dir = (cypher_dir or _DEFAULT_CYPHER_DIR).resolve()
        self._templates: dict[str, CypherTemplate] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self._dir.exists():
            raise TemplateLoadError(f"Cypher template directory not found: {self._dir}")
        cypher_files = sorted(self._dir.glob("*.cypher"))
        if not cypher_files:
            logger.warning("No .cypher files found in %s", self._dir)
        for cypher_path in cypher_files:
            name = cypher_path.stem
            meta_path = cypher_path.with_suffix(".meta.yaml")
            template = self._load_one(name, cypher_path, meta_path)
            self._templates[name] = template
            logger.debug("Loaded Cypher template '%s' (cost_class=%s)", name, template.cost_class)

    def _load_one(self, name: str, cypher_path: Path, meta_path: Path) -> CypherTemplate:
        if not meta_path.exists():
            raise TemplateLoadError(
                f"Missing meta file for template '{name}': expected {meta_path}"
            )
        cypher_text = cypher_path.read_text(encoding="utf-8").strip()
        if not cypher_text:
            raise TemplateLoadError(f"Empty .cypher file for template '{name}': {cypher_path}")

        raw_meta = yaml.safe_load(meta_path.read_bytes())
        if not isinstance(raw_meta, dict):
            raise TemplateLoadError(
                f"Template meta '{meta_path}' must be a YAML mapping, got {type(raw_meta).__name__}"
            )
        try:
            from pydantic import ValidationError as _VE

            meta = TemplateMeta.model_validate(raw_meta)
        except _VE as exc:
            raise TemplateLoadError(
                f"Template meta '{meta_path}' failed validation: {exc}"
            ) from exc

        return CypherTemplate(name=name, cypher_text=cypher_text, meta=meta)

    # ── Dict-like access ───────────────────────────────────────────────────────

    def __getitem__(self, name: str) -> CypherTemplate:
        try:
            return self._templates[name]
        except KeyError:
            raise TemplateNotFoundError(name, list(self._templates)) from None

    def __contains__(self, name: str) -> bool:
        return name in self._templates

    def __len__(self) -> int:
        return len(self._templates)

    def names(self) -> list[str]:
        return sorted(self._templates)

    def all(self) -> dict[str, CypherTemplate]:
        return dict(self._templates)
