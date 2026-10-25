"""Tests for the appointments store (modules/scheduling/appointments.py).

Pure queries and conflict checks over the core Appointment model, exercised
against a fresh in-memory chart. No GPU, no live LLM, no cloud."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hdh.modules.scheduling import appointments as store

_MON_9AM = datetime(2030, 1, 7, 9, 0)  # a Monday
_HALF_HOUR = timedelta(minutes=30)


@pytest.fixture
def sched_db():
    """A fresh in-memory chart with patients and providers."""
    from hdh.core.generators import build_dataset
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    session = get_session(get_engine(":memory:"))
    build_dataset(session, n_patients=2, years_of_history=1, verbose=False)
    yield session
    session.close()


def _ids(session) -> tuple[int, int]:
    from hdh.core.models import Patient, Provider

    return session.query(Patient).first().id, session.query(Provider).first().id


def _book(session, patient_id, provider_id, start, *, minutes=30, reason=None):
    appt = store.create(
        session,
        store.NewAppointment(
            patient_id, provider_id, start, start + timedelta(minutes=minutes), reason=reason
        ),
    )
    session.commit()
    return appt


def test_create_defaults_to_scheduled_and_is_findable(sched_db):
    patient_id, provider_id = _ids(sched_db)
    appt = _book(sched_db, patient_id, provider_id, _MON_9AM, reason="BP check")
    assert appt.id is not None and appt.status == "scheduled"
    assert appt.reason == "BP check"
    assert [a.id for a in store.find(sched_db, patient_id=patient_id)] == [appt.id]
    assert [a.id for a in store.find(sched_db, provider_id=provider_id, since=datetime(2030, 1, 1))] == [
        appt.id
    ]
    assert store.find(sched_db, patient_id=patient_id, until=datetime(2030, 1, 1)) == []


def test_overlapping_flags_a_double_book_but_not_back_to_back(sched_db):
    patient_id, provider_id = _ids(sched_db)
    _book(sched_db, patient_id, provider_id, _MON_9AM)  # 09:00–09:30
    overlap = store.overlapping(
        sched_db, provider_id, _MON_9AM + timedelta(minutes=15), _MON_9AM + timedelta(minutes=45)
    )
    assert len(overlap) == 1
    back_to_back = store.overlapping(
        sched_db, provider_id, _MON_9AM + _HALF_HOUR, _MON_9AM + timedelta(minutes=60)
    )
    assert back_to_back == []


def test_overlapping_ignores_cancelled_slots(sched_db):
    patient_id, provider_id = _ids(sched_db)
    appt = _book(sched_db, patient_id, provider_id, _MON_9AM)
    appt.status = "cancelled"
    sched_db.commit()
    assert store.overlapping(sched_db, provider_id, _MON_9AM, _MON_9AM + _HALF_HOUR) == []


def test_clinic_hours_warning_is_soft(sched_db):
    patient_id, provider_id = _ids(sched_db)
    # 3am: either outside recorded hours (a string note) or hours unknown (None).
    out = store.clinic_hours_warning(
        sched_db, provider_id, datetime(2030, 1, 7, 3, 0), datetime(2030, 1, 7, 3, 30)
    )
    assert out is None or isinstance(out, str)
