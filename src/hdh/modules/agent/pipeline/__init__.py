"""Stateful agent pipeline (LangGraph) — the production-style architecture.

Contrast with the simple tool-runner loop in ``chat.py``: here every concern
is an explicit, separately-testable stage in a state machine, and a response
is only streamed after it has been validated against tool evidence.

    request
       │
    ┌──▼───────────┐
    │   GATEWAY    │  composition root: wires client, tools, quota, graph
    └──▼───────────┘
    ┌──▼───────────┐
    │  GUARDRAILS  │  topic guard (small LLM, preconfigured topics)
    │              │  + daily token quota (input/output, persisted)
    └──▼───────────┘──── off-topic / over quota ──▶ polite rejection
    ┌──▼───────────┐
    │    INTENT    │  classify the ask, extract entities, sketch a plan
    └──▼───────────┘
    ┌──▼───────────┐
    │TOOL EXECUTOR │  the heart: has conversation context, the DB schema,
    │  (≤ 3 tries) │  and every tool; on retry it receives the validator's
    └──▼───────────┘◀────────────────┐  feedback about what failed
    ┌──▼───────────┐                 │
    │  ASSEMBLER   │  draft answer from tool evidence only               │
    └──▼───────────┘                 │
    ┌──▼───────────┐── invalid ──────┘  (hallucination / ungrounded claim)
    │  VALIDATOR   │
    └──▼───────────┘── valid ──▶ stream response (validated before emitted)

Dependencies are injected (``PipelineDeps``), so the entire graph — including
the retry loop — runs in tests with fake LLMs and no API key.
"""

from .gateway import Gateway
from .graph import build_graph
from .state import AgentState, PipelineConfig, PipelineDeps
from .tracing import StepRecord, TraceStore, TurnContext, instrument_deps

__all__ = [
    "AgentState",
    "Gateway",
    "PipelineConfig",
    "PipelineDeps",
    "StepRecord",
    "TraceStore",
    "TurnContext",
    "build_graph",
    "instrument_deps",
]
