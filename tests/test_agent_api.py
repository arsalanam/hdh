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
