"""Pipeline state and dependency contracts.

``AgentState`` is the single mutable record that flows through the graph —
each node reads what it needs and returns a partial update. ``PipelineConfig``
and ``PipelineDeps`` are immutable: configuration is data, and every external
capability (LLM calls, tool execution, quota) is an injected callable, which
is what makes the whole pipeline testable offline.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict

DEFAULT_ALLOWED_TOPICS = (
    "patients, cohorts, and clinical data in the synthetic family-medicine dataset",
    "care gaps, risk scores, visits, diagnoses, prescriptions, labs, vitals",
    "dataset statistics, SQL questions about the database, SOAP notes",
    "how the hdh tool itself works",
)


class AgentState(TypedDict, total=False):
    """The record each graph node reads from and writes to."""

    question: str
    history: list[str]  # brief "Q: ... / A: ..." lines from earlier turns
    rejected: str  # guardrail rejection reason; routes straight to END
    intent: dict  # {"intent": ..., "entities": [...], "plan": ...}
    findings: str  # tool executor's raw findings text
    evidence: list[dict]  # tool-call log: {"tool", "input", "result"}
    draft: str  # assembler's response
    verdict: dict  # {"valid": bool, "reason": str}
    feedback: str  # validator feedback fed to the executor on retry
    attempts: int  # executor attempts so far (capped by config)
    failed: bool  # exhausted retries without a valid response
    usage: dict  # {"input_tokens": n, "output_tokens": n} accumulated


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable pipeline configuration."""

    model: str = "claude-opus-5"
    guard_model: str = "claude-haiku-4-5"
    max_attempts: int = 3
    allowed_topics: tuple[str, ...] = DEFAULT_ALLOWED_TOPICS
    daily_input_tokens: int = 500_000
    daily_output_tokens: int = 100_000
    tool_result_cap: int = 6_000  # chars of any one tool result kept in context


@dataclass(frozen=True)
class PipelineDeps:
    """Everything the graph nodes need, injected by the gateway (or a test).

    Callable contracts (all usage dicts are {"input_tokens", "output_tokens"}):
      check_topic(question)           -> (allowed, topic_or_reason, usage)
      analyze_intent(question, hist)  -> (intent_dict, usage)
      run_tools(question, intent, feedback, hist)
                                      -> (findings, evidence_list, usage)
      assemble(question, findings, evidence) -> (draft, usage)
      validate(question, draft, evidence)    -> (valid, reason, usage)
      quota_check()                   -> rejection reason or None
      trace(stage, message)           -> None  (progress reporting)
    """

    config: PipelineConfig
    check_topic: Callable
    analyze_intent: Callable
    run_tools: Callable
    assemble: Callable
    validate: Callable
    quota_check: Callable = field(default=lambda: None)
    trace: Callable = field(default=lambda stage, message: None)


def add_usage(state: AgentState, usage: dict) -> dict:
    """Merge a node's token usage into the running total (returns new dict)."""
    total = dict(state.get("usage") or {"input_tokens": 0, "output_tokens": 0})
    total["input_tokens"] += usage.get("input_tokens", 0)
    total["output_tokens"] += usage.get("output_tokens", 0)
    return total
