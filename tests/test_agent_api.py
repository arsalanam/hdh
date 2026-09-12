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
    client = _client(lambda q, tid=None: {})
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["service"] == "hdh-agent-api"


def test_ask_returns_the_agents_answer_and_the_seam_fields():
    def fake_ask(question, thread_id=None):
        assert question == "Which patients have uncontrolled HTN?"
        return {
            "answer": "Three patients: MRN1, MRN2, MRN3.",
            "status": "validated",
            "intent": {"intent": "cohort_search", "entities": ["HTN"]},
            "verdict": {"valid": True, "reason": "grounded"},
            "usage": {"input_tokens": 900, "output_tokens": 120},
            "trace_id": "ab12cd34",
            "thread_id": thread_id,
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
        "thread_id": "t-1",
    }
    resp = _client(lambda q, tid=None: reject).post("/ask", json={"question": "best lasagna recipe?"})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"


def test_an_empty_question_is_refused():
    resp = _client(lambda q, tid=None: {}).post("/ask", json={"question": "   "})
    assert resp.status_code == 422


def test_a_missing_question_is_a_validation_error():
    resp = _client(lambda q, tid=None: {}).post("/ask", json={})
    assert resp.status_code == 422


# ── streaming (SSE) ──────────────────────────────────────────────────────


def test_ask_stream_emits_stage_frames_then_the_answer():
    def fake_stream(question, thread_id=None):
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
    def boom(question, thread_id=None):
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


def _run(run_id, source, title, turns, thread_id=None, started_at="2026-09-11 10:00:00"):
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "started_at": started_at,
        "source": source,
        "model": "claude-opus-4-8",
        "guard_model": "claude-haiku-4-5",
        "turns": turns,
        "title": title,
        "input_tokens": 100,
        "output_tokens": 20,
    }


def _client_with_store(store):
    return TestClient(
        create_app(ask=lambda q, tid=None: {}, stream=lambda q, tid=None: iter(()), store=store)
    )


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


# ── serving the built SPA ────────────────────────────────────────────────


def test_the_spa_is_served_and_api_routes_still_win(tmp_path):
    (tmp_path / "index.html").write_text("<div id='root'>hdh agent</div>", encoding="utf-8")
    app = create_app(
        ask=lambda q: {}, stream=lambda q: iter(()), store=_FakeStore([]), web_dist=str(tmp_path)
    )
    client = TestClient(app)
    # "/" serves the SPA
    root = client.get("/")
    assert root.status_code == 200 and "hdh agent" in root.text
    # but an API route mounted before the SPA still takes precedence
    assert client.get("/health").json()["status"] == "ok"


def test_no_spa_mount_when_not_built(tmp_path):
    """An unbuilt checkout (no index.html) serves no SPA — the API still runs."""
    app = create_app(
        ask=lambda q: {},
        stream=lambda q: iter(()),
        store=_FakeStore([]),
        web_dist=str(tmp_path / "absent"),
    )
    client = TestClient(app)
    assert client.get("/").status_code == 404  # nothing mounted at /
    assert client.get("/health").json()["status"] == "ok"  # API unaffected


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
    out = _normalize(state, run_id="abcdef123456", thread_id="thread-xyz")
    assert out["status"] == "validated"
    assert out["intent"] == {"intent": "risk"}
    assert out["trace_id"] == "abcdef12"  # first 8 of the run id
    assert out["thread_id"] == "thread-xyz"
    assert out["usage"]["output_tokens"] == 5


# ── threads (the foldable history tree) ──────────────────────────────────


def test_ask_threads_the_conversation():
    """A request's thread_id is carried through; omitting it starts a new one."""
    seen = {}

    def fake_ask(question, thread_id=None):
        seen["thread_id"] = thread_id
        return {"answer": "ok", "status": "validated", "trace_id": "aa", "thread_id": thread_id}

    client = TestClient(create_app(ask=fake_ask))
    # explicit thread_id is threaded through and echoed back
    body = client.post("/ask", json={"question": "q", "thread_id": "t-42"}).json()
    assert seen["thread_id"] == "t-42" and body["thread_id"] == "t-42"
    # omitting it starts a new thread (a non-empty id is generated and returned)
    body2 = client.post("/ask", json={"question": "q"}).json()
    assert body2["thread_id"] and body2["thread_id"] != "t-42"


def test_threads_groups_runs_newest_first():
    store = _FakeStore(
        [
            _run("r3", "ui", "third in A", 1, thread_id="A", started_at="2026-09-11 12:00:00"),
            _run("r2", "ui", "only in B", 1, thread_id="B", started_at="2026-09-11 11:00:00"),
            _run("r1", "ui", "first in A", 1, thread_id="A", started_at="2026-09-11 10:00:00"),
            _run("cli", "cli-chat", "terminal", 1, thread_id="Z"),  # hidden
            _run("legacy", "ui", "old run", 1, thread_id=None),  # its own single-run thread
        ]
    )
    body = _client_with_store(store).get("/threads").json()
    by_id = {t["thread_id"]: t for t in body}
    # thread A has both its runs, newest first; titled by the newest run
    assert [r["conversation_id"] for r in by_id["A"]["runs"]] == ["r3", "r1"]
    assert by_id["A"]["title"] == "third in A"
    # thread B has one run; the CLI run is excluded; the legacy run is its own thread
    assert [r["conversation_id"] for r in by_id["B"]["runs"]] == ["r2"]
    assert "Z" not in by_id
    assert "legacy" in by_id and by_id["legacy"]["runs"][0]["conversation_id"] == "legacy"
    # threads are ordered newest-first (A's newest run is 12:00, B's is 11:00)
    assert [t["thread_id"] for t in body][:2] == ["A", "B"]
