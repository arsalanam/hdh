"""Tests for the agent's scheduling tools (modules/scheduling/agent_tools.py).

They prove the guarantees that make agent-driven scheduling safe: booking refuses
a double-booking, cancelling marks the slot cancelled (not deleted), rescheduling
moves it and still refuses a clash, and an encounter can be linked to its
appointment. No GPU, no live LLM."""

from __future__ import annotations

import json

import pytest

from hdh.modules.scheduling.agent_tools import build_scheduling_tools

_TOOL_NAMES = {
    "find_appointments",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "link_encounter_to_appointment",
}


@pytest.fixture
def sched_db():
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


def _provider(session) -> str:
    from hdh.core.models import Provider

    return session.query(Provider).first().identifier


def _book(tools, mrn, provider, start="2030-01-07 09:00", **extra) -> dict:
    return json.loads(
        _tool(tools, "book_appointment").call({"mrn": mrn, "provider": provider, "start": start, **extra})
    )


def test_builder_returns_the_five_tools(sched_db):
    assert {t.name for t in build_scheduling_tools(sched_db, identity=None)} == _TOOL_NAMES


def test_builder_offers_nothing_without_a_session():
    assert build_scheduling_tools(None, identity=None) == []


def test_book_then_find(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    mrn = _mrn(sched_db)
    booked = _book(tools, mrn, _provider(sched_db), reason="BP check")
    assert booked["status"] == "scheduled" and booked["appointment_id"]
    listed = _tool(tools, "find_appointments").call({"mrn": mrn})
    assert f"#{booked['appointment_id']}" in listed and "BP check" in listed


def test_book_refuses_a_double_booking(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    mrn, provider = _mrn(sched_db), _provider(sched_db)
    _book(tools, mrn, provider, start="2030-01-07 09:00")
    clash = _tool(tools, "book_appointment").call(
        {"mrn": mrn, "provider": provider, "start": "2030-01-07 09:15"}
    )
    assert "double-booking refused" in clash


def test_book_refuses_unknown_patient_and_provider(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    assert "no patient" in _tool(tools, "book_appointment").call(
        {"mrn": "ZZZ999", "provider": _provider(sched_db), "start": "2030-01-07 09:00"}
    )
    assert "no provider" in _tool(tools, "book_appointment").call(
        {"mrn": _mrn(sched_db), "provider": "Nobody McNowhere", "start": "2030-01-07 09:00"}
    )


def test_book_rejects_a_bad_datetime(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    msg = _tool(tools, "book_appointment").call(
        {"mrn": _mrn(sched_db), "provider": _provider(sched_db), "start": "next tuesday"}
    )
    assert "ISO 8601" in msg


def test_cancel_marks_cancelled_not_deleted(sched_db):
    from hdh.core.models import Appointment

    tools = build_scheduling_tools(sched_db, identity=None)
    appt_id = _book(tools, _mrn(sched_db), _provider(sched_db))["appointment_id"]
    _tool(tools, "cancel_appointment").call({"appointment_id": appt_id, "reason": "patient request"})
    row = sched_db.get(Appointment, appt_id)
    assert (
        row.status == "cancelled" and row.cancel_reason == "patient request" and row.cancelled_at is not None
    )


def test_cancel_refuses_an_unknown_appointment(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    assert "no appointment" in _tool(tools, "cancel_appointment").call(
        {"appointment_id": 99999, "reason": "x"}
    )


def test_reschedule_moves_the_slot(sched_db):
    from hdh.core.models import Appointment

    tools = build_scheduling_tools(sched_db, identity=None)
    appt_id = _book(tools, _mrn(sched_db), _provider(sched_db), start="2030-01-07 09:00")["appointment_id"]
    _tool(tools, "reschedule_appointment").call(
        {"appointment_id": appt_id, "start": "2030-01-07 14:30", "reason": "clinic double-booked"}
    )
    assert sched_db.get(Appointment, appt_id).start_at.hour == 14


def test_reschedule_refuses_a_clash(sched_db):
    tools = build_scheduling_tools(sched_db, identity=None)
    mrn, provider = _mrn(sched_db), _provider(sched_db)
    _book(tools, mrn, provider, start="2030-01-07 09:00")  # 09:00–09:30
    second = _book(tools, mrn, provider, start="2030-01-07 11:00")["appointment_id"]  # 11:00–11:30
    msg = _tool(tools, "reschedule_appointment").call(
        {"appointment_id": second, "start": "2030-01-07 09:15", "reason": "move earlier"}
    )
    assert "double-booking refused" in msg


def test_link_encounter_to_appointment(sched_db):
    from hdh.core.models import Appointment, Visit

    tools = build_scheduling_tools(sched_db, identity=None)
    appt_id = _book(tools, _mrn(sched_db), _provider(sched_db))["appointment_id"]
    visit_id = sched_db.query(Visit).first().id
    _tool(tools, "link_encounter_to_appointment").call(
        {"visit_id": visit_id, "appointment_id": appt_id, "reason": "seen"}
    )
    assert sched_db.get(Visit, visit_id).appointment_id == appt_id
    # the appointment resolves back to the visit it fulfilled
    assert sched_db.get(Appointment, appt_id).visit.id == visit_id
