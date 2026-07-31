"""Graph assembly: wire the nodes into the LangGraph state machine.

guardrails ──rejected──▶ END
    │
 intent ─▶ tool_executor ─▶ assembler ─▶ validator ──valid──▶ END
                ▲                            │
                └────── invalid & tries left ┘   (exhausted ─▶ END, failed)
"""

from langgraph.graph import END, StateGraph

from .nodes import (
    make_assembler_node,
    make_executor_node,
    make_guardrails_node,
    make_intent_node,
    make_validator_node,
)
from .state import AgentState, PipelineDeps


def _after_guardrails(state: AgentState) -> str:
    return "rejected" if state.get("rejected") else "ok"


def _after_validator(state: AgentState) -> str:
    if state.get("verdict", {}).get("valid"):
        return "done"
    if state.get("failed"):
        return "done"
    return "retry"


def build_graph(deps: PipelineDeps):
    """Compile the pipeline graph with all dependencies injected."""
    graph = StateGraph(AgentState)
    graph.add_node("guardrails", make_guardrails_node(deps))
    graph.add_node("intent", make_intent_node(deps))
    graph.add_node("tool_executor", make_executor_node(deps))
    graph.add_node("assembler", make_assembler_node(deps))
    graph.add_node("validator", make_validator_node(deps))

    graph.set_entry_point("guardrails")
    graph.add_conditional_edges("guardrails", _after_guardrails, {"rejected": END, "ok": "intent"})
    graph.add_edge("intent", "tool_executor")
    graph.add_edge("tool_executor", "assembler")
    graph.add_edge("assembler", "validator")
    graph.add_conditional_edges("validator", _after_validator, {"retry": "tool_executor", "done": END})
    return graph.compile()
