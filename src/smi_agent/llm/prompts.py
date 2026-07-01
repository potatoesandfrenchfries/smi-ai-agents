"""Jinja2 prompt template loader with Anthropic cache_control support.

Templates live in ``prompts/<dir>/`` relative to the project root.  The
``prompt_templates_dir`` field in ``LLMConfig`` selects the subdirectory
(e.g. ``"prompts/uc02"``).

System prompt blocks are wrapped with ``cache_control: {"type": "ephemeral"}``
so that Anthropic's prompt caching reduces token spend on repeated invocations
with the same system context.

Usage::

    loader = PromptLoader("prompts/uc02")
    system_block = loader.render_system("system", {"investigation_id": "inv-001"})
    user_text = loader.render_user("plan_generation", {"exposure_id": "EXP-001", ...})
    messages = [{"role": "system", "content": [system_block]}, {"role": "user", "content": user_text}]
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

# ── Project root resolution ────────────────────────────────────────────────────

_DEFAULT_PROMPTS_ROOT = Path(__file__).parents[3]  # repo root


def _prompts_root() -> Path:
    """Return the root under which ``prompt_templates_dir`` is resolved.

    Override with ``SMI_PROMPTS_ROOT`` env var for containerised deployments.
    """
    env_root = os.environ.get("SMI_PROMPTS_ROOT")
    if env_root:
        return Path(env_root)
    return _DEFAULT_PROMPTS_ROOT


# ── Loader ─────────────────────────────────────────────────────────────────────


class PromptNotFoundError(KeyError):
    """Raised when a .j2 template file cannot be found in the templates dir."""


class PromptLoader:
    """Loads and renders Jinja2 templates from a prompt directory.

    Args:
        templates_dir: Relative path to the prompt directory (e.g. ``"prompts/uc02"``).
            Resolved against ``SMI_PROMPTS_ROOT`` (or project root).

    Raises:
        ValueError: If ``templates_dir`` is absolute or contains ``..``.
        FileNotFoundError: If the resolved directory does not exist.
    """

    def __init__(self, templates_dir: str = "prompts/default") -> None:
        p = Path(templates_dir)
        if p.is_absolute():
            raise ValueError(f"templates_dir must be relative, got: {templates_dir!r}")
        if ".." in p.parts:
            raise ValueError(f"templates_dir must not contain '..', got: {templates_dir!r}")

        resolved = (_prompts_root() / p).resolve()
        # Guard against symlink escape: ensure resolved path is still under prompts root.
        try:
            resolved.relative_to(_prompts_root().resolve())
        except ValueError as exc:
            raise ValueError(
                f"templates_dir resolves outside prompts root (possible symlink): {templates_dir!r}"
            ) from exc
        if not resolved.exists():
            raise FileNotFoundError(f"Prompt templates directory not found: {resolved}")

        self._resolved_dir = resolved
        self._env = Environment(
            loader=FileSystemLoader(str(resolved)),
            undefined=StrictUndefined,
            autoescape=False,
        )
        # Register tojson filter (mirrors Jinja2 standard)
        import json

        self._env.filters["tojson"] = lambda v, indent=None: json.dumps(
            v, indent=indent, default=str
        )

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render_user(self, template_name: str, context: dict[str, Any]) -> str:
        """Render a template and return plain text for the user role."""
        return self._render(template_name, context)

    def render_system(self, template_name: str, context: dict[str, Any]) -> dict[str, Any]:
        """Render a template and wrap in an Anthropic cache_control block.

        Returns a content block dict::

            {
                "type": "text",
                "text": "<rendered template>",
                "cache_control": {"type": "ephemeral"},
            }

        This enables Anthropic prompt caching (50% cache-hit discount).
        Non-Anthropic models receive the dict and the ``cache_control`` key
        is ignored by LiteLLM.
        """
        text = self._render(template_name, context)
        return {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }

    def _render(self, template_name: str, context: dict[str, Any]) -> str:
        filename = f"{template_name}.j2"
        try:
            tmpl = self._env.get_template(filename)
        except TemplateNotFound as exc:
            available = sorted(p.stem for p in self._resolved_dir.glob("*.j2"))
            raise PromptNotFoundError(
                f"Template '{template_name}' not found in {self._resolved_dir}. "
                f"Available: {available}"
            ) from exc
        return tmpl.render(**context)

    # ── Introspection ──────────────────────────────────────────────────────────

    def available_templates(self) -> list[str]:
        """Return sorted list of template names (without .j2 extension)."""
        return sorted(p.stem for p in self._resolved_dir.glob("*.j2"))
