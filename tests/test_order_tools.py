"""Phase-4 tests for the agent's order-drafting tools (agent/order_tools.py).

The agent drafts order changes from a recorded encounter; these prove the
guarantees that make that safe: everything is an UNRELEASED draft, a released
order is refused, cancelling a draft withdraws it, and prescribing a medication
needs the prescribing permission. No GPU, no live LLM."""

from __future__ import annotations

import json

import pytest

from hdh.core.identity import Identity
from hdh.modules.agent.order_tools import build_order_tools

_NURSE = Identity(subject="n", username="nurse.joy", roles=frozenset({"nurse"}))
_CLINICIAN = Identity(subject="c", username="dr.who", roles=frozenset({"clinician"}))
_NOBODY = Identity(subject="x", username="stranger", roles=frozenset())


@pytest.fixture
def order_db():
    """A fresh in-memory chart with a dataset (patients with visits)."""
    from hdh.core.generators import build_dataset
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    session = get_session(get_engine(":memory:"))
    build_dataset(session, n_patients=2, years_of_history=1, verbose=False)
    yield session
    session.close()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _mrn(session) -> str:
    from hdh.core.models import Patient

    return session.query(Patient).first().mrn


def _order_row(session, order_id: int):
    from hdh.core.models import ServiceRequest

    return session.get(ServiceRequest, order_id)


def _order_row_any(session, order_id: int):
    """Including voided rows (the default query filter hides them)."""
    from sqlalchemy import select

    from hdh.core.models import ServiceRequest

    return session.execute(
        select(ServiceRequest).where(ServiceRequest.id == order_id).execution_options(include_voided=True)
    ).scalar_one_or_none()


# ── draft_order ────────────────────────────────────────────────────────────────


def test_draft_order_creates_an_unreleased_draft(order_db):
    from hdh.core.models import RequestOrigin, RequestStatus, ServiceKind

    tools = build_order_tools(order_db, identity=None)
    out = json.loads(
        _tool(tools, "draft_order").call(
            {
                "mrn": _mrn(order_db),
                "kind": "medication",
                "display": "Amlodipine 5 mg daily",
                "sig": "1 tablet by mouth once daily",
                "reason": "new hypertension dx",
            }
        )
    )
    assert out["status"] == "draft"
    order = _order_row(order_db, out["drafted_order_id"])
    assert order.status is RequestStatus.DRAFT  # never ACTIVE — releasing is human
    assert order.origin is RequestOrigin.AGENT
    assert order.kind is ServiceKind.MEDICATION
    assert order.sig == "1 tablet by mouth once daily"
    assert order.visit_id is not None  # attached to the latest encounter


def test_draft_order_rejects_unknown_kind(order_db):
    tools = build_order_tools(order_db, identity=None)
    msg = _tool(tools, "draft_order").call(
        {"mrn": _mrn(order_db), "kind": "spell", "display": "Expelliarmus"}
    )
    assert "unknown order kind" in msg


def test_draft_order_rejects_bad_occurrence(order_db):
    tools = build_order_tools(order_db, identity=None)
    msg = _tool(tools, "draft_order").call(
        {"mrn": _mrn(order_db), "kind": "lab", "display": "A1c", "occurrence": "next tuesday"}
    )
    assert "must be a date" in msg


def test_draft_order_unknown_patient(order_db):
    tools = build_order_tools(order_db, identity=None)
    assert "no patient" in _tool(tools, "draft_order").call(
        {"mrn": "MRN00000000", "kind": "lab", "display": "A1c"}
    )


def test_a_lab_is_ordered_not_resulted(order_db):
    # The tool orders a test; it never carries a value. (A result comes only via
    # interchange — design §12. We assert the drafted row has no result fields.)
    from hdh.core.models import ServiceKind

    tools = build_order_tools(order_db, identity=None)
    out = json.loads(
        _tool(tools, "draft_order").call({"mrn": _mrn(order_db), "kind": "lab", "display": "HbA1c"})
    )
    order = _order_row(order_db, out["drafted_order_id"])
    assert order.kind is ServiceKind.LAB
    assert not order.fulfilled_by  # nothing resulted against it


# ── list_orders ────────────────────────────────────────────────────────────────


def test_list_orders_shows_drafts_with_ids(order_db):
    tools = build_order_tools(order_db, identity=None)
    mrn = _mrn(order_db)
    out = json.loads(_tool(tools, "draft_order").call({"mrn": mrn, "kind": "procedure", "display": "ECG"}))
    listed = _tool(tools, "list_orders").call({"mrn": mrn})
    assert f"#{out['drafted_order_id']}" in listed
    assert "[draft]" in listed and "ECG" in listed


# ── update_order ───────────────────────────────────────────────────────────────


def _draft(order_db, kind="lab", display="A1c") -> int:
    tools = build_order_tools(order_db, identity=None)
    out = json.loads(
        _tool(tools, "draft_order").call({"mrn": _mrn(order_db), "kind": kind, "display": display})
    )
    return out["drafted_order_id"]


def test_update_order_amends_a_draft(order_db):
    oid = _draft(order_db)
    tools = build_order_tools(order_db, identity=None)
    detail = _tool(tools, "update_order").call(
        {"order_id": oid, "reason": "clarify timing", "occurrence": "2025-01-15", "sig": "fasting"}
    )
    assert "sig" in detail or "occurrence" in detail  # the applied change is reported
    order = _order_row(order_db, oid)
    assert order.sig == "fasting"
    assert str(order.occurrence_date) == "2025-01-15"


def test_update_order_needs_a_field(order_db):
    oid = _draft(order_db)
    tools = build_order_tools(order_db, identity=None)
    assert "nothing to change" in _tool(tools, "update_order").call({"order_id": oid, "reason": "oops"})


def test_update_order_rejects_a_released_order(order_db):
    from hdh.core.models import RequestStatus

    oid = _draft(order_db)
    order = _order_row(order_db, oid)
    order.status = RequestStatus.ACTIVE  # a clinician released it
    order_db.commit()
    tools = build_order_tools(order_db, identity=None)
    msg = _tool(tools, "update_order").call({"order_id": oid, "reason": "too late", "sig": "changed"})
    assert "not a draft" in msg and "clinician" in msg
    assert _order_row(order_db, oid).sig != "changed"  # untouched


def test_update_order_unknown_id(order_db):
    tools = build_order_tools(order_db, identity=None)
    assert "no draft order" in _tool(tools, "update_order").call(
        {"order_id": 99999, "reason": "x", "sig": "y"}
    )


# ── cancel_order ───────────────────────────────────────────────────────────────


def test_cancel_order_withdraws_a_draft(order_db):
    oid = _draft(order_db)
    tools = build_order_tools(order_db, identity=None)
    detail = _tool(tools, "cancel_order").call({"order_id": oid, "reason": "dictated in error"})
    assert "voided" in detail.lower()
    assert _order_row_any(order_db, oid).voided_at is not None
    # it no longer shows among outstanding orders
    assert f"#{oid} " not in _tool(tools, "list_orders").call({"mrn": _mrn(order_db)})


def test_cancel_order_rejects_a_released_order(order_db):
    from hdh.core.models import RequestStatus

    oid = _draft(order_db)
    order = _order_row(order_db, oid)
    order.status = RequestStatus.ACTIVE
    order_db.commit()
    tools = build_order_tools(order_db, identity=None)
    msg = _tool(tools, "cancel_order").call({"order_id": oid, "reason": "stop it"})
    assert "not a draft" in msg
    assert _order_row(order_db, oid).voided_at is None  # a released order is not voided here


# ── permissions ──────────────────────────────────────────────────────────────


def test_a_nurse_may_draft_a_lab_but_not_a_medication(order_db):
    mrn = _mrn(order_db)
    tools = build_order_tools(order_db, identity=_NURSE)
    lab = _tool(tools, "draft_order").call({"mrn": mrn, "kind": "lab", "display": "CBC"})
    assert json.loads(lab)["status"] == "draft"  # chart:create — allowed
    med = _tool(tools, "draft_order").call({"mrn": mrn, "kind": "medication", "display": "Lisinopril 10 mg"})
    assert "medication:create" in med  # prescribing — refused


def test_a_clinician_may_draft_a_medication(order_db):
    tools = build_order_tools(order_db, identity=_CLINICIAN)
    out = _tool(tools, "draft_order").call(
        {"mrn": _mrn(order_db), "kind": "medication", "display": "Metformin 500 mg BID"}
    )
    assert json.loads(out)["status"] == "draft"


def test_an_unprivileged_identity_is_refused(order_db):
    tools = build_order_tools(order_db, identity=_NOBODY)
    assert "chart:create" in _tool(tools, "draft_order").call(
        {"mrn": _mrn(order_db), "kind": "lab", "display": "A1c"}
    )


# ── wiring ─────────────────────────────────────────────────────────────────────


def test_builder_offers_nothing_without_a_session():
    # Needs a session to read/write; absent one it returns [], so the always-on
    # tool baseline (build_tools(None)) is unchanged.
    assert build_order_tools(None) == []


def test_builder_exposes_the_four_tools(order_db):
    names = {t.name for t in build_order_tools(order_db, identity=None)}
    assert names == {"list_orders", "draft_order", "update_order", "cancel_order"}
