"""Offline tests for the LangGraph agent pipeline.

Every dependency is injected as a fake, so the full graph — guardrails,
intent, executor, assembler, validator, and the retry loop — runs without an
API key. This is the payoff of the PipelineDeps contract.
"""

import pytest

pytest.importorskip("langgraph")

from hdh.modules.agent.pipeline import (
    PipelineConfig,
    PipelineDeps,
    StepRecord,
    TraceStore,
    TurnContext,
    build_graph,
    instrument_deps,
)
from hdh.modules.agent.pipeline.gateway import Gateway

NO_USAGE = {"input_tokens": 10, "output_tokens": 5}


def make_deps(*, on_topic=True, quota_reason=None, verdicts=None, config=None):
    """Fake dependencies; `verdicts` is the sequence of validator outcomes."""
    verdicts = list(verdicts if verdicts is not None else [(True, "grounded")])
    calls = {"executor": 0, "validator": 0}

    def check_topic(question):
        return on_topic, "clinical query" if on_topic else "cooking recipes", NO_USAGE

    def analyze_intent(question, history):
        return {"intent": "cohort_search", "entities": ["HTN"], "plan": "query db"}, NO_USAGE

    def run_tools(question, intent, feedback, history):
        calls["executor"] += 1
        findings = f"attempt {calls['executor']} findings"
        if feedback:
            findings += f" (fixed: {feedback})"
        evidence = [{"tool": "query_database", "input": {"sql": "SELECT 1"}, "result": "[1]"}]
        return findings, evidence, NO_USAGE

    def assemble(question, findings, evidence):
        return f"ANSWER based on {findings}", NO_USAGE

    def validate(question, draft, evidence):
        index = min(calls["validator"], len(verdicts) - 1)
        calls["validator"] += 1
        valid, reason = verdicts[index]
        return valid, reason, NO_USAGE

    deps = PipelineDeps(
        config=config or PipelineConfig(max_attempts=3),
        check_topic=check_topic,
        analyze_intent=analyze_intent,
        run_tools=run_tools,
        assemble=assemble,
        validate=validate,
        quota_check=lambda: quota_reason,
    )
    return deps, calls


def run_question(deps, question="Which patients have uncontrolled HTN?"):
    return build_graph(deps).invoke({"question": question, "history": []})


def test_happy_path_single_attempt():
    deps, calls = make_deps()
    state = run_question(deps)
    assert state["verdict"]["valid"]
    assert state["attempts"] == 1
    assert calls["executor"] == 1
    assert "ANSWER" in Gateway.answer_of(state)


def test_validator_failure_triggers_retry_with_feedback():
    deps, calls = make_deps(verdicts=[(False, "MRN not in evidence"), (True, "grounded")])
    state = run_question(deps)
    assert state["attempts"] == 2
    assert calls["executor"] == 2
    assert state["verdict"]["valid"]
    # the retry's executor run saw the validator's feedback
    assert "fixed: MRN not in evidence" in state["findings"]


def test_retries_capped_at_max_attempts():
    deps, calls = make_deps(verdicts=[(False, "still hallucinating")])
    state = run_question(deps)
    assert calls["executor"] == 3  # max_attempts
    assert state["failed"]
    answer = Gateway.answer_of(state)
    assert "could not produce a fully validated answer" in answer
    assert "treat with caution" in answer


def test_off_topic_rejected_before_any_tools():
    deps, calls = make_deps(on_topic=False)
    state = run_question(deps, "Best lasagna recipe?")
    assert "rejected" in state
    assert calls["executor"] == 0
    assert "only help with" in Gateway.answer_of(state)


def test_quota_exhaustion_rejected_before_topic_llm():
    deps, calls = make_deps(quota_reason="daily input-token quota exhausted (500,000/500,000)")
    state = run_question(deps)
    assert "Usage limit" in state["rejected"]
    assert calls["executor"] == 0


def test_usage_accumulates_across_nodes():
    deps, _ = make_deps(verdicts=[(False, "x"), (True, "grounded")])
    state = run_question(deps)
    # guard + intent + 2×executor + 2×assembler + 2×validator = 8 calls × 10/5
    assert state["usage"]["input_tokens"] == 80
    assert state["usage"]["output_tokens"] == 40


def make_store(tmp_path) -> TraceStore:
    return TraceStore(f"sqlite:///{(tmp_path / 'traces.db').as_posix()}")


def test_trace_store_run_turn_step_lifecycle(tmp_path):
    store = make_store(tmp_path)
    run_id = store.start_run(source="test", model="m", guard_model="g", max_attempts=3)
    turn_id = store.start_turn(run_id, 1, "Q?")
    store.record_step(
        StepRecord(turn_id, 1, "guardrails", 1, "ok", {"question": "Q?"}, {"allowed": True}, 10, 5, 12)
    )
    store.end_turn(turn_id, "validated", "A.", 1, {"input_tokens": 10, "output_tokens": 5})

    detail = store.run_detail(run_id[:8])
    assert detail["run_id"] == run_id
    turn = detail["turns"][0]
    assert turn["status"] == "validated"
    assert turn["steps"][0]["stage"] == "guardrails"
    assert turn["steps"][0]["input"] == {"question": "Q?"}


def test_trace_store_quota_from_recorded_steps(tmp_path):
    store = make_store(tmp_path)
    run_id = store.start_run(source="test", model="m", guard_model="g", max_attempts=3)
    turn_id = store.start_turn(run_id, 1, "Q?")
    assert store.check_quota(100, 50) is None
    store.record_step(StepRecord(turn_id, 1, "tool-executor", 1, "ok", None, None, 90, 40, 5))
    assert store.daily_usage() == (90, 40)
    store.record_step(StepRecord(turn_id, 2, "assembler", 1, "ok", None, None, 15, 5, 5))
    assert "input-token quota exhausted" in store.check_quota(100, 50)


def test_instrumented_graph_records_every_step_and_retry(tmp_path):
    store = make_store(tmp_path)
    run_id = store.start_run(source="test", model="m", guard_model="g", max_attempts=3)
    ctx = TurnContext(turn_id=store.start_turn(run_id, 1, "Q?"))

    deps, _ = make_deps(verdicts=[(False, "MRN not in evidence"), (True, "grounded")])
    state = build_graph(instrument_deps(deps, store, ctx)).invoke({"question": "Q?", "history": []})
    store.end_turn(ctx.turn_id, "validated", "A.", state["attempts"], state["usage"])

    detail = store.run_detail(run_id[:8])
    steps = detail["turns"][0]["steps"]
    stages = [s["stage"] for s in steps]
    # guardrails, intent, then two full executor→assembler→validator cycles
    assert stages == [
        "guardrails",
        "intent",
        "tool-executor",
        "assembler",
        "validator",
        "tool-executor",
        "assembler",
        "validator",
    ]
    validator_steps = [s for s in steps if s["stage"] == "validator"]
    assert validator_steps[0]["status"] == "invalid"
    assert validator_steps[1]["status"] == "ok"
    # attempt numbers advance with the retry
    assert [s["attempt"] for s in steps if s["stage"] == "tool-executor"] == [1, 2]
    # every step carries token usage; the daily total matches the state total
    assert store.daily_usage() == (state["usage"]["input_tokens"], state["usage"]["output_tokens"])


def test_selective_tool_exposure_by_intent():
    from hdh.modules.agent.tools import build_tools

    scoped = build_tools(None, include={"get_risk_scores", "query_database", "search_patients"})
    assert {t.name for t in scoped} == {"get_risk_scores", "query_database", "search_patients"}
    everything = build_tools(None)
    assert len(everything) == 6


def test_selective_schema_revealing():
    from hdh.modules.agent.tools import build_tools

    scoped = build_tools(None, tables=("patients", "visits"))
    sql_tool = next(t for t in scoped if t.name == "query_database")
    desc = sql_tool.to_dict()["description"]
    assert "patients(" in desc and "visits(" in desc
    assert "lab_results(" not in desc and "prescriptions(" not in desc


def test_tool_result_clipping():
    from hdh.modules.agent.tools import clip_tool_results

    response = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "x" * 10_000},
            {"type": "tool_result", "tool_use_id": "b", "content": "short"},
        ],
    }
    clipped = clip_tool_results(response, cap=6_000)
    big, small = clipped["content"]
    assert len(big["content"]) < 6_300
    assert "truncated 4,000 chars" in big["content"]
    assert small["content"] == "short"
    assert clip_tool_results(None, cap=10) is None
