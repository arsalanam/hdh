"""The gateway: single entry point and composition root for the pipeline.

Everything that touches the outside world is constructed here — the Anthropic
client, the database tools, the quota store — and handed to the graph as
injected callables. Nodes stay pure; tests replace these implementations with
fakes (see tests/test_pipeline.py).
"""

import json
import os
import re
from pathlib import Path

from hdh.modules.agent.agent import DEFAULT_MODEL, SYSTEM_PROMPT

from .graph import build_graph
from .state import PipelineConfig, PipelineDeps
from .tracing import TraceStore, TurnContext, instrument_deps

GUARD_PROMPT = """\
You are a topic gatekeeper for a clinical-data assistant over a SYNTHETIC
family-medicine dataset. Allowed topics:
{topics}

Reply with exactly one line:
ALLOWED: <three-word topic label>     — if the question fits the topics
OFF_TOPIC: <three-word reason>        — otherwise\
"""

INTENT_PROMPT = """\
Classify a question for a clinical-data agent: the intent category, the
clinical entities mentioned (MRNs, conditions, age groups, ...), and a
one-sentence plan for which tools to use.\
"""

VALIDATOR_PROMPT = """\
You are a response validator. Given a QUESTION, a DRAFT answer, and the TOOL
EVIDENCE it must be based on, check every specific claim in the draft: MRNs,
names, ages, counts, values, and conditions must appear in (or be directly
computable from) the evidence. General clinical phrasing is fine; invented
specifics are not.\
"""

# Schema-enforced verdict: the model cannot return unparseable output.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["valid", "reason"],
    "additionalProperties": False,
}

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["patient_lookup", "cohort_search", "risk", "care_gaps", "stats", "sql", "other"],
        },
        "entities": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "string"},
    },
    "required": ["intent", "entities", "plan"],
    "additionalProperties": False,
}


def _usage_of(message) -> dict:
    """Extract {input_tokens, output_tokens} from an API response."""
    u = getattr(message, "usage", None)
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
    }


def _first_json(text: str) -> dict:
    """Parse the first JSON object found in a model reply (tolerant)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except ValueError:
        return {}


def _text_of(message) -> str:
    """Concatenate the text blocks of an API response."""
    return "\n".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def default_trace_url() -> str:
    """Trace DB location: HDH_TRACE_DB (any SQLAlchemy URL, e.g. postgresql://...)
    or a local SQLite file at ~/.hdh/traces.db."""
    url = os.environ.get("HDH_TRACE_DB")
    if url:
        return url
    path = Path.home() / ".hdh" / "traces.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


class Gateway:
    """Front door: builds the pipeline once, then answers questions through it."""

    def __init__(
        self,
        db_session,
        model: str | None = None,
        max_attempts: int = 3,
        trace=None,
        source: str = "pipeline",
    ):
        """Wire client, tools, trace store, and graph (the composition root).

        Starting a gateway session opens a new *run* in the trace database;
        every question becomes a *turn*, and every component execution a
        *step* (see tracing.py).
        """
        import anthropic

        from hdh.modules.agent.tools import build_tools

        self.config = PipelineConfig(
            model=model or os.environ.get("HDH_AGENT_MODEL", DEFAULT_MODEL),
            guard_model=os.environ.get("HDH_GUARD_MODEL", "claude-haiku-4-5"),
            max_attempts=max_attempts,
            daily_input_tokens=int(os.environ.get("HDH_QUOTA_INPUT_TOKENS", 500_000)),
            daily_output_tokens=int(os.environ.get("HDH_QUOTA_OUTPUT_TOKENS", 100_000)),
        )
        self.client = anthropic.Anthropic()
        self.tools = build_tools(db_session)
        self.trace_store = TraceStore(default_trace_url())
        self.run_id = self.trace_store.start_run(
            source=source,
            model=self.config.model,
            guard_model=self.config.guard_model,
            max_attempts=max_attempts,
        )
        self._ctx = TurnContext()
        self._turn_count = 0
        self.history: list[str] = []
        self._trace = trace or (lambda stage, message: print(f"  ├─ {stage:<14} {message}"))
        self.graph = build_graph(
            instrument_deps(
                PipelineDeps(
                    config=self.config,
                    check_topic=self._check_topic,
                    analyze_intent=self._analyze_intent,
                    run_tools=self._run_tools,
                    assemble=self._assemble,
                    validate=self._validate,
                    quota_check=lambda: self.trace_store.check_quota(
                        self.config.daily_input_tokens, self.config.daily_output_tokens
                    ),
                    trace=self._trace,
                ),
                self.trace_store,
                self._ctx,
            )
        )

    # ── Real dependency implementations (injected into the graph) ────────────

    def _check_topic(self, question: str) -> tuple[bool, str, dict]:
        """Topic guard on the small model; cheap and preconfigured."""
        prompt = GUARD_PROMPT.format(topics="\n".join(f"- {t}" for t in self.config.allowed_topics))
        message = self.client.messages.create(
            model=self.config.guard_model,
            max_tokens=64,
            system=prompt,
            messages=[{"role": "user", "content": question}],
        )
        reply = _text_of(message).strip()
        allowed = reply.upper().startswith("ALLOWED")
        label = reply.split(":", 1)[-1].strip() if ":" in reply else reply
        return allowed, label, _usage_of(message)

    def _analyze_intent(self, question: str, history: list[str]) -> tuple[dict, dict]:
        """Intent classification on the small model."""
        context = ("Recent conversation:\n" + "\n".join(history[-6:]) + "\n\n") if history else ""
        message = self.client.messages.create(
            model=self.config.guard_model,
            max_tokens=300,
            system=INTENT_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": INTENT_SCHEMA}},
            messages=[{"role": "user", "content": context + question}],
        )
        return _first_json(_text_of(message)) or {"intent": "other"}, _usage_of(message)

    def _run_tools(
        self, question: str, intent: dict, feedback: str, history: list[str]
    ) -> tuple[str, list[dict], dict]:
        """The executor: main model + all tools, aware of retry feedback."""
        parts = [SYSTEM_PROMPT]
        if intent:
            parts.append(f"Intent analysis of this request: {json.dumps(intent)}")
        if history:
            parts.append("Recent conversation:\n" + "\n".join(history[-6:]))
        if feedback:
            parts.append(
                "IMPORTANT: your previous answer FAILED validation for this reason: "
                f"{feedback}\nGather the evidence needed to fix that — verify every "
                "specific value with tools this time."
            )
        runner = self.client.beta.messages.tool_runner(
            model=self.config.model,
            max_tokens=16000,
            system="\n\n".join(parts),
            tools=self.tools,
            messages=[{"role": "user", "content": question}],
        )
        findings, evidence, usage = "", [], {"input_tokens": 0, "output_tokens": 0}
        for message in runner:
            for key, value in _usage_of(message).items():
                usage[key] += value
            for block in message.content:
                if block.type == "tool_use":
                    evidence.append({"tool": block.name, "input": dict(block.input)})
            texts = [b.text for b in message.content if b.type == "text"]
            if texts:
                findings = "\n".join(texts)
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                blocks = tool_response.get("content") or []
                results: list[dict] = (
                    [dict(b) for b in blocks if isinstance(b, dict)] if not isinstance(blocks, str) else []
                )
                for i, result_block in enumerate(results):
                    result = result_block.get("content", "")
                    text = result if isinstance(result, str) else str(result)
                    slot = len(evidence) - len(results) + i
                    if 0 <= slot < len(evidence):
                        evidence[slot]["result"] = text[:1200]
        return findings, evidence, usage

    def _assemble(self, question: str, findings: str, evidence: list[dict]) -> tuple[str, dict]:
        """Draft the final response strictly from the executor's evidence."""
        message = self.client.messages.create(
            model=self.config.model,
            max_tokens=2048,
            system=(
                "Assemble the final answer for the care team using ONLY the "
                "executor findings and tool evidence provided. Cite MRNs. Be "
                "concise and lead with the answer. Do not add any fact that is "
                "not in the evidence."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"QUESTION:\n{question}\n\nEXECUTOR FINDINGS:\n{findings}\n\n"
                        f"TOOL EVIDENCE:\n{json.dumps(evidence, indent=1)[:20000]}"
                    ),
                }
            ],
        )
        return _text_of(message), _usage_of(message)

    def _validate(self, question: str, draft: str, evidence: list[dict]) -> tuple[bool, str, dict]:
        """Verdict on the draft: grounded in evidence, or retry with reason."""
        message = self.client.messages.create(
            model=self.config.model,
            max_tokens=500,
            system=VALIDATOR_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"QUESTION:\n{question}\n\nDRAFT:\n{draft}\n\n"
                        f"TOOL EVIDENCE:\n{json.dumps(evidence, indent=1)[:20000]}"
                    ),
                }
            ],
        )
        verdict = _first_json(_text_of(message))
        return (
            bool(verdict.get("valid")),
            str(verdict.get("reason", "validator reply unparseable")),
            _usage_of(message),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def ask(self, question: str) -> dict:
        """Run one question through the pipeline as a traced turn."""
        used_in, used_out = self.trace_store.daily_usage()
        self._trace(
            "gateway",
            f"run {self.run_id[:8]} · quota today: "
            f"{max(0, self.config.daily_input_tokens - used_in):,} input / "
            f"{max(0, self.config.daily_output_tokens - used_out):,} output tokens left",
        )
        self._turn_count += 1
        turn_id = self.trace_store.start_turn(self.run_id, self._turn_count, question)
        self._ctx.turn_id, self._ctx.seq, self._ctx.attempt = turn_id, 0, 0
        try:
            state = self.graph.invoke({"question": question, "history": list(self.history)})
        except Exception as error:
            self.trace_store.end_turn(turn_id, "error", f"{type(error).__name__}: {error}", 0, {})
            raise
        answer = self.answer_of(state)
        status = (
            "rejected" if state.get("rejected") else "unvalidated" if state.get("failed") else "validated"
        )
        self.trace_store.end_turn(
            turn_id,
            status=status,
            answer=answer,
            attempts=state.get("attempts", 0),
            usage=state.get("usage") or {},
            final_state={
                "intent": state.get("intent"),
                "verdict": state.get("verdict"),
                "rejected": state.get("rejected"),
                "failed": state.get("failed"),
            },
        )
        self._trace("gateway", f"turn {self._turn_count} recorded as {status} (turn id {turn_id})")
        self.history.append(f"Q: {question}")
        self.history.append(f"A: {answer[:400]}")
        return state

    @staticmethod
    def answer_of(state: dict) -> str:
        """The user-facing text for a finished pipeline state."""
        if state.get("rejected"):
            return state["rejected"]
        if state.get("failed"):
            return (
                "I could not produce a fully validated answer after "
                f"{state.get('attempts', 0)} attempts. Last issue: "
                f"{state.get('verdict', {}).get('reason', 'unknown')}. "
                "Here is the unvalidated draft — treat with caution:\n\n" + state.get("draft", "(no draft)")
            )
        return state.get("draft", "(no response)")
