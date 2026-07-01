"""BaseSpecialist — interface every specialist agent implements.

Specialists are registered with the AgentRegistry and called by the
Supervisor via tool-calling. Each specialist has its own tools, prompt,
LLM lane, and budget — all configured via YAML.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from smi_agent.agents.response import StructuredResponse
from smi_agent.streaming import StepEmitter


class BaseSpecialist(ABC):
    """Interface for specialist agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique specialist name (matches YAML file: specialist_<name>)."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description (used as tool description for supervisor)."""

    @abstractmethod
    async def run(
        self,
        query: str,
        context: dict[str, Any],
        step_emitter: StepEmitter,
    ) -> StructuredResponse:
        """Execute the specialist's ReAct loop and return structured response.

        Args:
            query: The user's question/request.
            context: Shared context (tenant_id, entity_id, conversation_id, etc.)
            step_emitter: For streaming thinking steps to the client.
        """

    def as_tool_definition(self) -> dict[str, Any]:
        """Return OpenAI function tool definition for the supervisor LLM."""
        return {
            "type": "function",
            "function": {
                "name": f"ask_{self.name}",
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The specific question or task for this specialist",
                        },
                    },
                    "required": ["query"],
                },
            },
        }
