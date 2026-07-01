"""Startup graph-schema validator.

At worker startup, ``validate_templates_against_schema()`` cross-references every
loaded template's ``graph_touches`` (labels, relationships, indexes) against the
declared Neo4j schema in ``schema/neo4j_schema.yaml``.

Any reference to an undeclared label, relationship, or index is a hard error —
the worker refuses to start so drift is caught at deploy time, not at request time.

Usage (worker bootstrap, M8)::

    from smi_agent.neo4j_client.schema_validator import validate_templates_against_schema

    validate_templates_against_schema(template_loader, schema_path)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from smi_agent.neo4j_client.templates import TemplateLoader

logger = logging.getLogger(__name__)

_DEFAULT_SCHEMA_PATH = Path(
    os.environ.get(
        "SMI_SCHEMA_PATH",
        str(Path(__file__).parents[4] / "schema" / "neo4j_schema.yaml"),
    )
)


class SchemaValidationError(RuntimeError):
    """Raised when a template references a graph element not in the schema."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        bullet = "\n  - ".join(violations)
        super().__init__(
            f"Schema validation failed — {len(violations)} violation(s):\n  - {bullet}\n"
            "Update schema/neo4j_schema.yaml or fix the template meta files."
        )


def load_neo4j_schema(schema_path: Path | None = None) -> dict[str, set[str]]:
    """Load ``schema/neo4j_schema.yaml`` and return sets by category.

    Returns:
        Dict with keys ``"labels"``, ``"relationships"``, ``"indexes"`` — each
        value is a ``set[str]``.
    """
    path = (schema_path or _DEFAULT_SCHEMA_PATH).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Neo4j schema file not found: {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_bytes()) or {}
    return {
        "labels": set(raw.get("labels", [])),
        "relationships": set(raw.get("relationships", [])),
        "indexes": set(raw.get("indexes", [])),
    }


def validate_templates_against_schema(
    loader: TemplateLoader,
    schema_path: Path | None = None,
) -> None:
    """Validate every loaded template's graph_touches against the schema.

    Args:
        loader: A fully loaded ``TemplateLoader``.
        schema_path: Path to ``neo4j_schema.yaml``.  Defaults to
            ``schema/neo4j_schema.yaml`` relative to the project root.

    Raises:
        SchemaValidationError: One or more templates reference undeclared elements.
        FileNotFoundError: Schema file does not exist.
    """
    schema = load_neo4j_schema(schema_path)
    violations: list[str] = []

    for name, template in loader.all().items():
        gt = template.graph_touches

        for label in gt.labels:
            if label not in schema["labels"]:
                violations.append(f"template '{name}': label '{label}' not in schema")
        for rel in gt.relationships:
            if rel not in schema["relationships"]:
                violations.append(f"template '{name}': relationship '{rel}' not in schema")
        for idx in gt.indexes:
            if idx not in schema["indexes"]:
                violations.append(f"template '{name}': index '{idx}' not in schema")

    if violations:
        raise SchemaValidationError(violations)

    logger.info(
        "Schema validation passed: %d template(s) checked against %s labels, "
        "%s relationships, %s indexes.",
        len(loader),
        len(schema["labels"]),
        len(schema["relationships"]),
        len(schema["indexes"]),
    )
