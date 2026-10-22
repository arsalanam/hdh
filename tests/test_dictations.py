"""Phase-2 tests for the dictation store (design media-io-service.md §3).

Three layers, none needing a GPU or a live LLM:
  * the pure store (save / latest / get, provider+patient scoping, soft-delete),
  * the injectable summary seam (fake client; failure never blocks a save),
  * the agent tool ``use_last_dictation`` and the ``/dictations`` HTTP surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hdh.core.identity import Identity
from hdh.modules.comprehension.dictations import (
    DictationError,
    NewDictation,
    get_dictation,
    latest_dictation,
    llm_summarizer,
    save_dictation,
)


@pytest.fixture
def dictation_db():
    """A fresh in-memory chart with the dictations table and a small dataset —
    its own engine, isolated from the session-scoped fixture."""
    from hdh.core.generators import build_dataset
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()  # idempotent; ensures `dictations` is in Base.metadata
    session = get_session(get_engine(":memory:"))
    build_dataset(session, n_patients=2, years_of_history=1, verbose=False)
    yield session
    session.close()


# ── the pure store ────────────────────────────────────────────────────────────


def test_save_then_latest_round_trips(dictation_db):
    did = save_dictation(
        dictation_db,
        NewDictation(
            text="Patient reports a productive cough for three days.",
            provider_id=1001,
            source="encounter",
            segments=[
                {"speaker": "Provider", "text": "How long?"},
                {"speaker": "Patient", "text": "Three days"},
            ],
            model="null-echo",
            duration_s=12.5,
            needs_review=False,
            summary="Cough x3 days.",
        ),
    )
    dictation_db.commit()
    latest = latest_dictation(dictation_db, provider_id=1001)
    assert latest["dictation_id"] == did
    assert latest["summary"] == "Cough x3 days."
    assert latest["source"] == "encounter"
    assert latest["speakers"] == ["Provider", "Patient"]
    assert latest["chars"] == len("Patient reports a productive cough for three days.")
    assert latest["model"] == "null-echo" and latest["duration_s"] == 12.5


def test_latest_is_the_most_recent_for_that_provider(dictation_db):
    save_dictation(dictation_db, NewDictation(text="first", provider_id=1001, summary="a"))
    second = save_dictation(dictation_db, NewDictation(text="second", provider_id=1001, summary="b"))
    other = save_dictation(dictation_db, NewDictation(text="theirs", provider_id=2002, summary="c"))
    dictation_db.commit()
    # the provider's own newest, never another provider's
    assert latest_dictation(dictation_db, provider_id=1001)["dictation_id"] == second
    assert latest_dictation(dictation_db, provider_id=2002)["dictation_id"] == other


def test_latest_narrows_to_a_bound_patient(dictation_db):
    from hdh.core.models import Patient

    pa, pb = dictation_db.query(Patient).limit(2).all()
    save_dictation(
        dictation_db, NewDictation(text="about A", provider_id=1001, patient_id=pa.id, summary="A")
    )
    for_b = save_dictation(
        dictation_db, NewDictation(text="about B", provider_id=1001, patient_id=pb.id, summary="B")
    )
    dictation_db.commit()
    # unscoped → the newest overall; scoped → that patient's
    assert latest_dictation(dictation_db, provider_id=1001)["dictation_id"] == for_b
    scoped = latest_dictation(dictation_db, provider_id=1001, patient_id=pa.id)
    assert scoped["text"] == "about A"


def test_voided_dictations_are_invisible(dictation_db):
    from datetime import datetime

    from sqlalchemy import update

    from hdh.core.models import Base

    did = save_dictation(dictation_db, NewDictation(text="recorded in error", provider_id=1001, summary="x"))
    dictation_db.commit()
    assert latest_dictation(dictation_db, provider_id=1001) is not None
    table = Base.metadata.tables["dictations"]
    dictation_db.execute(update(table).where(table.c.id == did).values(voided_at=datetime.now()))
    dictation_db.commit()
    assert latest_dictation(dictation_db, provider_id=1001) is None
    assert get_dictation(dictation_db, did) is None


def test_get_is_scoped_to_the_owning_provider(dictation_db):
    did = save_dictation(dictation_db, NewDictation(text="mine", provider_id=1001, summary="m"))
    dictation_db.commit()
    assert get_dictation(dictation_db, did, provider_id=1001)["text"] == "mine"
    # another provider cannot read it by guessing the id
    assert get_dictation(dictation_db, did, provider_id=2002) is None


# ── the summary seam ───────────────────────────────────────────────────────────


def _fake_client(text="Chief complaint: cough.", boom=False):
    def create(**_kwargs):
        if boom:
            raise RuntimeError("transport error")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_summarizer_returns_the_models_text():
    summarize = llm_summarizer(client=_fake_client("Cough x3 days; afebrile; rest advised."))
    assert summarize("long transcript ...") == "Cough x3 days; afebrile; rest advised."


def test_summarizer_is_none_on_empty_or_failure():
    assert llm_summarizer(client=_fake_client())("   ") is None  # empty transcript
    # a transport error must not raise — a summary is best-effort, the save is not
    assert llm_summarizer(client=_fake_client(boom=True))("something") is None


# ── the agent tool ─────────────────────────────────────────────────────────────


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_use_last_dictation_returns_the_source(dictation_db):
    from hdh.core.models import Patient
    from hdh.modules.comprehension.dictations import build_dictation_tools

    patient = dictation_db.query(Patient).first()
    save_dictation(
        dictation_db,
        NewDictation(
            text="Follow-up for hypertension; BP 128/82.",
            provider_id=1001,
            patient_id=patient.id,
            source="encounter",
            summary="HTN follow-up, BP 128/82.",
        ),
    )
    dictation_db.commit()
    identity = Identity(subject="sub-1", username="dr.x", provider_id=1001)
    tools = build_dictation_tools(dictation_db, identity=identity)
    assert {t.name for t in tools} == {"use_last_dictation"}

    out = json.loads(_tool(tools, "use_last_dictation").call({}))
    assert out["summary"] == "HTN follow-up, BP 128/82."
    assert out["text"].startswith("Follow-up for hypertension")
    assert out["mrn"] == patient.mrn  # enriched so the agent can chart against it

    # scoped by MRN
    scoped = json.loads(_tool(tools, "use_last_dictation").call({"mrn": patient.mrn}))
    assert scoped["dictation_id"] == out["dictation_id"]


def test_use_last_dictation_is_honest_when_there_is_none(dictation_db):
    from hdh.modules.comprehension.dictations import build_dictation_tools

    identity = Identity(subject="sub-1", username="dr.x", provider_id=1001)
    tools = build_dictation_tools(dictation_db, identity=identity)
    msg = _tool(tools, "use_last_dictation").call({})
    assert "No recorded dictation" in msg  # a refusal, not an invented transcript


def test_use_last_dictation_refuses_an_unknown_mrn(dictation_db):
    from hdh.modules.comprehension.dictations import build_dictation_tools

    identity = Identity(subject="sub-1", username="dr.x", provider_id=1001)
    tools = build_dictation_tools(dictation_db, identity=identity)
    assert "No patient with MRN" in _tool(tools, "use_last_dictation").call({"mrn": "MRN00000000"})


# ── the HTTP surface (fake backend, no DB) ──────────────────────────────────────

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hdh.modules.agent_api import create_app  # noqa: E402
from hdh.modules.agent_api.auth import AuthError  # noqa: E402

_OK = Identity(subject="sub-test", username="dr.test", roles=frozenset({"clinician"}))


def _ALLOW(_header):
    return _OK


def _DENY(_header):
    raise AuthError("no token")


class _FakeDictations:
    """A stand-in for the dictation backend with the three methods the API uses."""

    def __init__(self, *, current=None, fetched=None, raise_on_save=False):
        self.saved = None
        self.saved_identity = None
        self._current = current
        self._fetched = fetched
        self._raise = raise_on_save

    def save(self, request, *, identity=None):
        if self._raise:
            raise DictationError("no patient with MRN MRNZZZ")
        self.saved = request
        self.saved_identity = identity
        return {
            "dictation_id": 7,
            "created_at": "2026-10-20T10:00:00",
            "source": request.source,
            "mrn": request.mrn,
            "summary": "a short summary",
            "needs_review": request.needs_review,
            "chars": len(request.text),
        }

    def current(self, *, identity=None, mrn=None):
        return self._current

    def get(self, dictation_id, *, identity=None):
        return self._fetched


def _app(dictations, authenticator=_ALLOW):
    return TestClient(
        create_app(
            ask=lambda q, tid=None, identity=None: {}, dictations=dictations, authenticator=authenticator
        )
    )


def test_post_dictation_persists_and_summarizes():
    fake = _FakeDictations()
    resp = _app(fake).post(
        "/dictations",
        json={"text": "Patient reports a cough.", "mrn": "MRN1", "source": "encounter", "model": "null-echo"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dictation_id"] == 7 and body["summary"] == "a short summary"
    # the body's `model` maps to the STT-model field, not the summary model
    assert fake.saved.stt_model == "null-echo"
    assert fake.saved.mrn == "MRN1" and fake.saved_identity is _OK


def test_post_dictation_rejects_empty_text():
    assert _app(_FakeDictations()).post("/dictations", json={"text": ""}).status_code == 422


def test_post_dictation_unknown_patient_is_422():
    resp = _app(_FakeDictations(raise_on_save=True)).post("/dictations", json={"text": "x", "mrn": "MRNZZZ"})
    assert resp.status_code == 422


def test_current_dictation_returns_the_last_one():
    detail = {"dictation_id": 7, "source": "encounter", "summary": "s", "text": "full text", "mrn": "MRN1"}
    body = _app(_FakeDictations(current=detail)).get("/dictations/current").json()
    assert body["dictation_id"] == 7 and body["text"] == "full text"


def test_current_dictation_404_when_none():
    assert _app(_FakeDictations(current=None)).get("/dictations/current").status_code == 404


def test_get_dictation_by_id_and_404():
    detail = {"dictation_id": 9, "source": "dictation", "text": "t"}
    assert _app(_FakeDictations(fetched=detail)).get("/dictations/9").json()["dictation_id"] == 9
    assert _app(_FakeDictations(fetched=None)).get("/dictations/9").status_code == 404


def test_dictation_endpoints_require_a_token():
    client = _app(_FakeDictations(current={"dictation_id": 1, "source": "x"}), authenticator=_DENY)
    assert client.post("/dictations", json={"text": "x"}).status_code == 401
    assert client.get("/dictations/current").status_code == 401
    assert client.get("/dictations/1").status_code == 401


def test_real_backend_round_trips_over_http(tmp_path):
    """The REAL facade (not a fake) over HTTP: POST persists through the store,
    GET current and GET by id read it back, and patient-scoping holds — proving
    the full wiring with a stubbed summarizer (no LLM). "Run it before you
    document it": the fake-backend tests above do not exercise this path."""
    from hdh.core.generators import build_dataset
    from hdh.core.models import Patient, Provider, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.comprehension.dictations import dictation_backend

    bootstrap_schema()
    db_path = str(tmp_path / "integ.db")
    setup = get_session(get_engine(db_path))
    build_dataset(setup, n_patients=2, years_of_history=1, verbose=False)
    patients = setup.query(Patient).limit(2).all()
    mrn, other_mrn = patients[0].mrn, patients[1].mrn
    provider_id = setup.query(Provider).first().id
    setup.close()

    identity = Identity(subject="sub-9", username="dr.real", provider_id=provider_id)
    backend = dictation_backend(db_path=db_path, summarize=lambda _t: "stub summary")
    client = TestClient(
        create_app(
            ask=lambda q, tid=None, identity=None: {}, dictations=backend, authenticator=lambda _h: identity
        )
    )

    saved = client.post(
        "/dictations", json={"text": "Cough and fever since Monday.", "mrn": mrn, "source": "encounter"}
    ).json()
    assert saved["dictation_id"] >= 1 and saved["summary"] == "stub summary" and saved["mrn"] == mrn

    current = client.get("/dictations/current").json()
    assert current["dictation_id"] == saved["dictation_id"]
    assert current["text"] == "Cough and fever since Monday." and current["mrn"] == mrn

    fetched = client.get(f"/dictations/{saved['dictation_id']}").json()
    assert fetched["text"] == "Cough and fever since Monday."

    # scoped to a different patient → that patient has no dictation → 404
    assert client.get("/dictations/current", params={"mrn": other_mrn}).status_code == 404
