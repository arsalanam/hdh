"""Graph nodes, built by factories that receive their dependencies.

Each factory returns a plain ``callable(state) -> partial-state-update`` —
exactly what LangGraph expects — with every external capability coming from
the injected ``PipelineDeps``. No node constructs a client, opens a session,
or reads the environment.
"""

from .state import AgentState, PipelineDeps, add_usage


def make_guardrails_node(deps: PipelineDeps):
    """Input tier: daily quota first (free), then the topic-guard LLM."""

    def guardrails(state: AgentState) -> dict:
        """Reject over-quota or off-topic questions before any real work."""
        quota_reason = deps.quota_check()
        if quota_reason:
            deps.trace("guardrails", f"REJECTED — {quota_reason}")
            return {"rejected": f"Usage limit: {quota_reason}."}

        allowed, topic, usage = deps.check_topic(state["question"])
        if not allowed:
            deps.trace("guardrails", f"REJECTED — off-topic ({topic})")
            topics = "; ".join(deps.config.allowed_topics)
            return {
                "rejected": (f"I can only help with: {topics}. Your question was assessed as: {topic}."),
                "usage": add_usage(state, usage),
            }
        deps.trace("guardrails", f"topic allowed ✓ ({topic})")
        return {"usage": add_usage(state, usage)}

    return guardrails


def make_intent_node(deps: PipelineDeps):
    """Intent analysis: classify the ask and sketch a tool plan."""

    def intent(state: AgentState) -> dict:
        """Produce {"intent", "entities", "plan"} for the executor."""
        result, usage = deps.analyze_intent(state["question"], state.get("history") or [])
        deps.trace(
            "intent",
            f"{result.get('intent', '?')} · entities: {', '.join(result.get('entities') or []) or '—'}",
        )
        return {"intent": result, "usage": add_usage(state, usage)}

    return intent


def make_executor_node(deps: PipelineDeps):
    """The heart of the agent: context + schema + every tool, retry-aware."""

    def executor(state: AgentState) -> dict:
        """Run tools to gather evidence; on retry, address validator feedback."""
        attempt = state.get("attempts", 0) + 1
        feedback = state.get("feedback", "")
        deps.trace(
            "tool-executor",
            f"attempt {attempt}/{deps.config.max_attempts}"
            + (f" · addressing: {feedback[:90]}" if feedback else ""),
        )
        findings, evidence, usage = deps.run_tools(
            state["question"], state.get("intent") or {}, feedback, state.get("history") or []
        )
        deps.trace("tool-executor", f"{len(evidence)} tool call(s) recorded")
        return {
            "findings": findings,
            "evidence": evidence,
            "attempts": attempt,
            "usage": add_usage(state, usage),
        }

    return executor


def make_assembler_node(deps: PipelineDeps):
    """Response assembly: turn tool evidence into the drafted answer."""

    def assembler(state: AgentState) -> dict:
        """Draft the response strictly from findings + evidence."""
        draft, usage = deps.assemble(
            state["question"], state.get("findings", ""), state.get("evidence") or []
        )
        deps.trace("assembler", f"drafted {len(draft.split())}-word response")
        return {"draft": draft, "usage": add_usage(state, usage)}

    return assembler


def make_validator_node(deps: PipelineDeps):
    """Response validation: block hallucinations before anything is streamed."""

    def validator(state: AgentState) -> dict:
        """Check the draft against evidence; set verdict and retry feedback."""
        valid, reason, usage = deps.validate(
            state["question"], state.get("draft", ""), state.get("evidence") or []
        )
        update: dict = {
            "verdict": {"valid": valid, "reason": reason},
            "usage": add_usage(state, usage),
        }
        if valid:
            deps.trace("validator", "VALID ✓ — response is grounded in tool evidence")
        elif state.get("attempts", 0) >= deps.config.max_attempts:
            deps.trace("validator", f"INVALID after final attempt — {reason[:90]}")
            update["failed"] = True
        else:
            deps.trace("validator", f"INVALID — {reason[:90]} → retrying executor")
            update["feedback"] = reason
        return update

    return validator
