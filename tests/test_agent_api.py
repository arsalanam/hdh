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


# ── conversation history ─────────────────────────────────────────────────


class _FakeStore:
    """A stand-in for TraceStore with the two read methods the API uses."""

    def __init__(self, runs):
        self._runs = runs

    def recent_runs(self, limit=15):
        return self._runs[:limit]

    def run_detail(self, run_prefix):
        for r in self._runs:
            if r["run_id"].startswith(run_prefix):
                return r
        return None


def _run(run_id, source, title, turns):
    return {
        "run_id": run_id,
        "started_at": "2026-09-11 10:00:00",
        "source": source,
        "model": "claude-opus-4-8",
        "guard_model": "claude-haiku-4-5",
        "turns": turns,
        "title": title,
        "input_tokens": 100,
        "output_tokens": 20,
    }


def _client_with_store(store):
    return TestClient(create_app(ask=lambda q: {}, stream=lambda q: iter(()), store=store))


def test_conversations_lists_only_ui_runs_newest_first():
    store = _FakeStore(
        [
            _run("ui-2", "ui", "who is overdue?", turns=1),
            _run("cli-1", "cli-chat", "from the terminal", turns=3),  # must be hidden
            _run("ui-1", "ui", "is the diabetes controlled?", turns=2),
        ]
    )
    body = _client_with_store(store).get("/conversations").json()
    ids = [c["conversation_id"] for c in body]
    assert ids == ["ui-2", "ui-1"]  # CLI run excluded, UI order preserved
    assert body[0]["title"] == "who is overdue?" and body[0]["turns"] == 1


def test_a_conversation_transcript_returns_its_turns():
    detail = _run("ui-9", "ui", "chart this note", turns=2)
    detail["turns"] = [
        {
            "turn_index": 0,
            "question": "chart this note",
            "answer": "Done.",
            "status": "validated",
            "input_tokens": 50,
            "output_tokens": 10,
            "steps": [],
        },
        {
            "turn_index": 1,
            "question": "what changed?",
            "answer": "BP added.",
            "status": "validated",
            "input_tokens": 40,
            "output_tokens": 8,
            "steps": [],
        },
    ]
    body = _client_with_store(_FakeStore([detail])).get("/conversations/ui-9").json()
    assert body["conversation_id"] == "ui-9"
    assert [t["question"] for t in body["turns"]] == ["chart this note", "what changed?"]
    assert body["turns"][0]["answer"] == "Done."


def test_a_cli_run_is_not_reachable_as_a_ui_conversation():
    """Source-scoping is a boundary: the UI cannot read a CLI run's transcript."""
    store = _FakeStore([_run("cli-7", "cli-chat", "terminal q", turns=1)])
    assert _client_with_store(store).get("/conversations/cli-7").status_code == 404


def test_an_unknown_conversation_is_404():
    assert _client_with_store(_FakeStore([])).get("/conversations/nope").status_code == 404


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
