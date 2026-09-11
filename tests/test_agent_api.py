"""The agent HTTP API — Phase 1 (design docs/design/agentic-ui-module.md).

The surface over Gateway.ask(): POST /ask and /health. The agent backend is
injected as a fake `ask`, so these exercise the HTTP layer — request
validation, the response shape, the trace id — without an Anthropic key or a
live model. The seam itself (Gateway) is covered by the pipeline tests.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from hdh.modules.agent_api import create_app


def _client(ask):
    return TestClient(create_app(ask=ask))


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    import json

    out = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        out.append((event, data))
    return out


def test_health_names_the_service():
    client = _client(lambda q: {})
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["service"] == "hdh-agent-api"


def test_ask_returns_the_agents_answer_and_the_seam_fields():
    def fake_ask(question):
        assert question == "Which patients have uncontrolled HTN?"
        return {
            "answer": "Three patients: MRN1, MRN2, MRN3.",
            "status": "validated",
            "intent": {"intent": "cohort_search", "entities": ["HTN"]},
            "verdict": {"valid": True, "reason": "grounded"},
            "usage": {"input_tokens": 900, "output_tokens": 120},
            "trace_id": "ab12cd34",
        }

    resp = _client(fake_ask).post("/ask", json={"question": "Which patients have uncontrolled HTN?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"].startswith("Three patients")
    assert body["status"] == "validated"
    assert body["intent"]["intent"] == "cohort_search"
    assert body["verdict"]["valid"] is True
    assert body["usage"]["input_tokens"] == 900
    assert body["trace_id"] == "ab12cd34"


def test_a_rejected_turn_is_reported_not_hidden():
    """Off-topic / quota rejections come back as a normal 200 with status
    'rejected' — the front door presents them, it does not invent an error."""
    reject = {
        "answer": "I can only help with the clinical dataset.",
        "status": "rejected",
        "intent": None,
        "verdict": None,
        "usage": {},
        "trace_id": "deadbeef",
    }
    resp = _client(lambda q: reject).post("/ask", json={"question": "best lasagna recipe?"})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"


def test_an_empty_question_is_refused():
    resp = _client(lambda q: {}).post("/ask", json={"question": "   "})
    assert resp.status_code == 422


def test_a_missing_question_is_a_validation_error():
    resp = _client(lambda q: {}).post("/ask", json={})
    assert resp.status_code == 422


# ── streaming (SSE) ──────────────────────────────────────────────────────


def test_ask_stream_emits_stage_frames_then_the_answer():
    def fake_stream(question):
        assert question == "who is overdue?"
        yield ("stage", {"stage": "guardrails", "label": "Checking the question…"})
        yield ("stage", {"stage": "intent", "label": "Thinking…"})
        yield ("stage", {"stage": "tool-executor", "label": "Fetching data…"})
        yield ("stage", {"stage": "validator", "label": "Checking the answer…"})
        yield ("answer", {"answer": "Two patients.", "status": "validated", "trace_id": "ab12cd34"})

    client = TestClient(create_app(stream=fake_stream))
    resp = client.post("/ask/stream", json={"question": "who is overdue?"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.text)
    kinds = [e for e, _ in frames]
    assert kinds == ["stage", "stage", "stage", "stage", "answer"]
    # the stages carry human labels, in order
    assert [d["label"] for e, d in frames if e == "stage"][:2] == ["Checking the question…", "Thinking…"]
    # the final frame is the validated answer with the seam fields
    answer = frames[-1][1]
    assert answer["answer"] == "Two patients." and answer["trace_id"] == "ab12cd34"


def test_ask_stream_surfaces_an_error_frame():
    def boom(question):
        yield ("stage", {"stage": "intent", "label": "Thinking…"})
        yield ("error", {"detail": "RuntimeError: model unavailable"})

    resp = TestClient(create_app(stream=boom)).post("/ask/stream", json={"question": "x"})
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "error" and "model unavailable" in frames[-1][1]["detail"]


def test_ask_stream_refuses_an_empty_question():
    resp = TestClient(create_app(stream=lambda q: iter(()))).post("/ask/stream", json={"question": " "})
    assert resp.status_code == 422


def test_stage_labels_are_friendly_and_fall_back():
    from hdh.modules.agent_api.server import _label

    assert _label("tool-executor") == "Fetching data…"
    assert _label("assembler") == "Assembling the answer…"
    # an unmapped stage still reads as words, not a slug
    assert _label("some_other_stage") == "Some other stage"


def test_normalize_reduces_pipeline_state_to_the_seam():
    """The default backend maps a raw pipeline state to the response shape;
    check that reduction directly (no Anthropic, no gateway)."""
    from hdh.modules.agent_api.server import _normalize

    state = {
        "answers": ["…"],
        "attempts": 2,
        "failed": False,
        "rejected": None,
        "intent": {"intent": "risk"},
        "verdict": {"valid": True, "reason": "grounded"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    # Gateway.answer_of reads the finished state; a rejected/failed field drives
    # status. Here nothing is rejected or failed → validated.
    out = _normalize(state, run_id="abcdef123456")
    assert out["status"] == "validated"
    assert out["intent"] == {"intent": "risk"}
    assert out["trace_id"] == "abcdef12"  # first 8 of the run id
    assert out["usage"]["output_tokens"] == 5
