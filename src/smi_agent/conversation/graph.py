"""Build the LangGraph conversation graph.

When tool_calling is enabled, the graph includes a ReAct agent loop:
  START → context_inject → (compact | react_loop) → react_loop → generate → END

When disabled (default), the original 3-node flow is used:
  START → context_inject → (compact | generate) → generate → END
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from langgraph.graph import END, START, StateGraph

from smi_agent.config.models import AgentDefinition
from smi_agent.conversation.nodes.compact import make_compact_node
from smi_agent.conversation.nodes.context_inject import make_context_inject_node
from smi_agent.conversation.nodes.generate import make_generate_node
from smi_agent.conversation.state import ConversationState
from smi_agent.llm.prompts import PromptLoader
from smi_agent.neo4j_client.driver import Neo4jDriver
from smi_agent.neo4j_client.templates import TemplateLoader

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def build_conversation_graph(
    defn: AgentDefinition,
    driver: Neo4jDriver,
    template_loader: TemplateLoader,
    checkpointer: Any = None,
    pg_client: Any = None,
) -> CompiledStateGraph:
    prompt_loader = PromptLoader(defn.llm.prompt_templates_dir)

    context_inject = make_context_inject_node(
        defn=defn, driver=driver, template_loader=template_loader
    )
    compact = make_compact_node(defn=defn, prompt_loader=prompt_loader)
    generate = make_generate_node(defn=defn, prompt_loader=prompt_loader)

    graph = StateGraph(ConversationState)
    graph.add_node("context_inject", context_inject)
    graph.add_node("compact", compact)
    graph.add_node("generate", generate)

    use_tools = defn.tool_calling.enabled

    if use_tools:
        # Import here to avoid circular deps when tools are disabled
        from smi_agent.conversation.nodes.react_loop import make_react_loop_node

        react_loop = make_react_loop_node(
            defn=defn,
            driver=driver,
            template_loader=template_loader,
            pg_client=pg_client,
            prompt_loader=prompt_loader,
        )
        graph.add_node("react_loop", react_loop)

        # Flow: context_inject → (compact → react_loop | react_loop) → generate
        def _route_with_tools(
            state: ConversationState,
        ) -> Literal["compact", "react_loop"]:
            return "compact" if state.get("should_compact") else "react_loop"

        graph.add_edge(START, "context_inject")
        graph.add_conditional_edges(
            "context_inject",
            _route_with_tools,
            {"compact": "compact", "react_loop": "react_loop"},
        )
        graph.add_edge("compact", "react_loop")
        graph.add_edge("react_loop", "generate")
        graph.add_edge("generate", END)

        logger.info(
            "Conversation graph: context_inject → react_loop (%d tools) → generate",
            len(defn.tool_calling.enabled_tools),
        )
    else:
        # Original 3-node flow (no tool calling)
        def _route_no_tools(
            state: ConversationState,
        ) -> Literal["compact", "generate"]:
            return "compact" if state.get("should_compact") else "generate"

        graph.add_edge(START, "context_inject")
        graph.add_conditional_edges(
            "context_inject",
            _route_no_tools,
            {"compact": "compact", "generate": "generate"},
        )
        graph.add_edge("compact", "generate")
        graph.add_edge("generate", END)

    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
