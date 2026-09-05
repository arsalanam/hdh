"""A medication fill leaves a trail (attribution A2).

Measured before this: `record_fill` wrote a `MedicationDispense` carrying
`origin=agent` and **no audit event**. So the row knew an agent filled it
and `chart_audit_events` did not — and "everything an agent did to this
patient" missed medication fills, the action with the most direct physical
consequence.

A fill is provenance, not yet a person: the actor is the origin and
`provider_id` is None until identity lands (AU2). Recording the trail now
and the name later is the right order — the event that never happened
cannot be backfilled.
"""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture()
def order(tmp_path):
    """A patient with one open, refillable medication order."""
    from hdh.core.models import (
        Base,
        Patient,
        RequestOrigin,
        RequestStatus,
        ServiceKind,
        ServiceRequest,
        Visit,
        VisitType,
        get_engine,
        get_session,
    )
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    patient = Patient(mrn="MRN1", first_name="T", last_name="P", date_of_birth=date(1970, 1, 1), sex="F")
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 1, 1), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    req = ServiceRequest(
        patient_id=patient.id,
        visit_id=visit.id,
        kind=ServiceKind.MEDICATION,
        display="Atorvastatin 20mg",
        status=RequestStatus.ACTIVE,
        origin=RequestOrigin.CLINICIAN,
        requested_date=date(2026, 1, 1),
        refills_authorised=3,
    )
    session.add(req)
    session.commit()
    yield session, patient, req
    session.close()
    engine.dispose()


def _events(session, patient):
    from hdh.core.models import ChartAuditEvent

    return session.query(ChartAuditEvent).filter_by(patient_id=patient.id, entity="MedicationDispense").all()


# ── the trail now records the fill ───────────────────────────────────────


def test_a_fill_writes_an_audit_event(order):
    from hdh.core.models import RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    decision, dispense = record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT)
    session.commit()
    assert dispense is not None
    events = _events(session, patient)
    assert len(events) == 1
    assert events[0].row_id == dispense.id


def test_the_event_names_the_agent_as_the_actor(order):
    from hdh.core.models import EditSource, RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT)
    session.commit()
    event = _events(session, patient)[0]
    assert event.actor_source == EditSource.AGENT
    assert "agent" in event.actor_name


def test_the_event_carries_what_was_filled(order):
    from hdh.core.models import RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT, days_supply=28)
    session.commit()
    after = _events(session, patient)[0].after
    assert after["drug_name"] == "Atorvastatin 20mg"
    assert after["dispensed_date"] == "2026-02-01"
    assert after["days_supply"] == 28
    assert after["origin"] == "agent"


def test_a_refused_fill_writes_nothing(order):
    """The dispense is None when refused, and there is no event to explain a
    fill that did not happen. The refusal is the whole outcome."""
    from hdh.core.models import RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    # Exhaust the authorisation: 1 initial + 3 refills = 4 fills, then refuse.
    for month in range(2, 6):
        record_fill(session, req, date(2026, month, 1), origin=RequestOrigin.AGENT)
    session.commit()
    before = len(_events(session, patient))
    decision, dispense = record_fill(session, req, date(2026, 7, 1), origin=RequestOrigin.AGENT)
    session.commit()
    assert dispense is None
    assert len(_events(session, patient)) == before, "a refusal must not leave a trail"


def test_each_fill_is_its_own_event(order):
    from hdh.core.models import RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT)
    record_fill(session, req, date(2026, 3, 1), origin=RequestOrigin.AGENT)
    session.commit()
    assert len(_events(session, patient)) == 2


# ── it shows up where a reader looks ─────────────────────────────────────


def test_the_fill_appears_in_the_chart_history(order):
    """The whole point: `hdh chart history` and the agent's `chart_history`
    read `chart_audit_events`, so a fill now surfaces beside every other
    change to the patient."""
    from hdh.core.models import ChartAuditEvent, RequestOrigin
    from hdh.core.refills import record_fill

    session, patient, req = order
    record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT)
    session.commit()
    trail = (
        session.query(ChartAuditEvent)
        .filter_by(patient_id=patient.id)
        .order_by(ChartAuditEvent.occurred_at.desc())
        .all()
    )
    assert any(e.entity == "MedicationDispense" and e.action.value == "create" for e in trail)


# ── provenance that is not a chart edit is not attributed ────────────────


def test_a_generated_origin_is_not_audited(order):
    """Bulk construction is the chart, not a change to it. A generated fill
    never reaches record_fill in practice — the generator bulk-inserts — but
    if one did, inventing an edit-surface for it would be worse than silence."""
    from hdh.core.refills import record_fill

    session, patient, req = order
    record_fill(session, req, date(2026, 2, 1), origin="generated")
    session.commit()
    assert _events(session, patient) == []


def test_the_dispense_still_carries_its_origin(order):
    """A2 adds the trail; it does not remove the row-level provenance, which
    is what makes a fill useful at the point of use."""
    from hdh.core.models import RequestOrigin
    from hdh.core.refills import record_fill

    session, _patient, req = order
    _decision, dispense = record_fill(session, req, date(2026, 2, 1), origin=RequestOrigin.AGENT)
    session.commit()
    assert dispense.origin == "agent"
