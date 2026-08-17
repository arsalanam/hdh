"""Chart amendment with an append-only audit trail (issue #40).

The contract under test: **one sanctioned mutation path**, every landed
change audited in the same transaction, voided rows invisible to readers,
refusals returned as outcomes rather than raised — and real deletion
available only through the admin purge path.
"""

from datetime import date, datetime

import pytest

from hdh.core.chartedit import (
    Actor,
    ChartEdit,
    EditAction,
    apply_edits,
    history,
    purge_visit,
    spec_for,
)
from hdh.core.models import (
    Allergy,
    ChartAuditEvent,
    Condition,
    ConditionStatus,
    EditSource,
    Patient,
    Prescription,
    Sex,
    Visit,
    VisitType,
    Vital,
    get_engine,
    get_session,
)
from hdh.core.schema_registry import bootstrap_schema

ACTOR = Actor(name="Dr. Test", source=EditSource.CLI)


@pytest.fixture()
def chart(tmp_path):
    """A patient with one visit and one of everything voidable."""
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chartedit.db"))
    session = get_session(engine)
    patient = Patient(
        mrn="MRN00EDIT01",
        first_name="Edit",
        last_name="Case",
        date_of_birth=date(1975, 6, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 5, 5), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add_all(
        [
            Condition(
                patient_id=patient.id,
                visit_id=visit.id,
                icd10_code="I10",
                description="Essential hypertension",
                status=ConditionStatus.ACTIVE,
                onset_date=date(2026, 5, 5),
            ),
            Prescription(visit_id=visit.id, drug_name="Lisinopril", dose="10mg", frequency="daily"),
            Vital(visit_id=visit.id, bp_systolic=152, bp_diastolic=94, heart_rate=88),
            Allergy(patient_id=patient.id, substance="Penicillin", reaction="rash"),
        ]
    )
    session.commit()
    yield session, patient, visit
    session.close()
    engine.dispose()


def _only(outcomes):
    assert len(outcomes) == 1
    return outcomes[0]


# ── amend ────────────────────────────────────────────────────────────


def test_amend_records_a_field_level_diff(chart):
    session, patient, _ = chart
    condition = session.query(Condition).first()
    outcome = _only(
        apply_edits(
            session,
            ACTOR,
            [
                ChartEdit(
                    "Condition",
                    condition.id,
                    EditAction.AMEND,
                    {"status": "resolved", "resolved_date": "2026-06-01"},
                    reason="symptoms settled",
                )
            ],
        )
    )
    assert outcome.applied and outcome.audit_id
    assert session.get(Condition, condition.id).status is ConditionStatus.RESOLVED

    event = session.get(ChartAuditEvent, outcome.audit_id)
    assert event.action.value == "amend" and event.reason == "symptoms settled"
    assert event.patient_id == patient.id and event.entity == "Condition"
    assert event.before["status"] == "active" and event.after["status"] == "resolved"
    assert event.after["resolved_date"] == "2026-06-01"  # coerced from text, stored as ISO
    assert "status" not in (set(event.after) - set(event.before))  # both sides of every field


def test_amend_refusals_are_outcomes_not_exceptions(chart):
    session, _, _ = chart
    condition = session.query(Condition).first()

    unknown_field = _only(
        apply_edits(
            session, ACTOR, [ChartEdit("Condition", condition.id, EditAction.AMEND, {"mrn": "X"}, "why")]
        )
    )
    assert not unknown_field.applied and "not amendable" in unknown_field.detail

    no_reason = _only(
        apply_edits(
            session, ACTOR, [ChartEdit("Condition", condition.id, EditAction.AMEND, {"chronic": "true"})]
        )
    )
    assert not no_reason.applied and "require a reason" in no_reason.detail

    missing_row = _only(apply_edits(session, ACTOR, [ChartEdit("Condition", 9999, EditAction.VOID, {}, "x")]))
    assert not missing_row.applied and "no Condition #9999" in missing_row.detail

    unknown_entity = _only(apply_edits(session, ACTOR, [ChartEdit("Patient", 1, EditAction.VOID, {}, "x")]))
    assert not unknown_entity.applied and "not an amendable entity" in unknown_entity.detail

    # nothing was audited for any refusal
    assert session.query(ChartAuditEvent).count() == 0


def test_vitals_need_no_reason_clinical_rows_do(chart):
    session, _, _ = chart
    vital = session.query(Vital).first()
    outcome = _only(
        apply_edits(session, ACTOR, [ChartEdit("Vital", vital.id, EditAction.AMEND, {"heart_rate": "72"})])
    )
    assert outcome.applied  # transcription fix, no reason required (§7 Q4)
    assert session.get(Vital, vital.id).heart_rate == 72
    assert spec_for("Vital").reason_required is False
    assert spec_for("Prescription").reason_required is True


# ── void ─────────────────────────────────────────────────────────────


def test_voided_rows_disappear_from_the_chart(chart):
    session, patient, _ = chart
    rx_id, mrn = session.query(Prescription).first().id, patient.mrn
    outcome = _only(
        apply_edits(
            session, ACTOR, [ChartEdit("Prescription", rx_id, EditAction.VOID, reason="entered in error")]
        )
    )
    assert outcome.applied

    # invisible to ORM reads — query, get, and relationship traversal alike.
    # expunge first: the filter is a query-time criterion, so a row already
    # in this session's identity map is answered from cache — see
    # visibility.py. Every CLI run and agent tool call gets a fresh session.
    session.expunge_all()
    assert session.query(Prescription).count() == 0
    assert session.get(Prescription, rx_id) is None
    reloaded = session.query(Patient).filter(Patient.mrn == mrn).one()
    assert [p for v in reloaded.visits for p in v.prescriptions] == []

    # but recoverable deliberately, and the audit event keeps its referent
    from sqlalchemy import select

    row = session.execute(
        select(Prescription).where(Prescription.id == rx_id).execution_options(include_voided=True)
    ).scalar_one()
    assert row.voided_at is not None
    assert session.query(ChartAuditEvent).filter_by(entity="Prescription").one().action.value == "void"


def test_voiding_a_visit_cascades_to_what_it_owns(chart):
    session, patient, visit = chart
    outcome = _only(
        apply_edits(session, ACTOR, [ChartEdit("Visit", visit.id, EditAction.VOID, reason="duplicate")])
    )
    assert outcome.applied and "owned rows" in outcome.detail
    assert session.query(Visit).count() == 0
    assert session.query(Condition).count() == 0
    assert session.query(Vital).count() == 0
    # the allergy is the patient's, not the visit's — it survives
    assert session.query(Allergy).count() == 1

    events = {(e.entity, e.action.value) for e in history(session, patient.id)}
    assert ("Visit", "void") in events and ("Condition", "void") in events

    repeat = _only(
        apply_edits(session, ACTOR, [ChartEdit("Visit", visit.id, EditAction.VOID, reason="again")])
    )
    assert not repeat.applied and "already voided" in repeat.detail


def test_voided_rows_are_not_amendable(chart):
    session, _, _ = chart
    allergy = session.query(Allergy).first()
    apply_edits(session, ACTOR, [ChartEdit("Allergy", allergy.id, EditAction.VOID, reason="wrong patient")])
    outcome = _only(
        apply_edits(
            session,
            ACTOR,
            [ChartEdit("Allergy", allergy.id, EditAction.AMEND, {"severity": "mild"}, "fix")],
        )
    )
    assert not outcome.applied and "voided" in outcome.detail


# ── dry run, history, purge ──────────────────────────────────────────


def test_dry_run_writes_nothing_at_all(chart):
    session, _, visit = chart
    outcomes = apply_edits(
        session,
        ACTOR,
        [ChartEdit("Visit", visit.id, EditAction.VOID, reason="considering it")],
        dry_run=True,
    )
    assert _only(outcomes).applied and _only(outcomes).detail.startswith("[dry run]")
    assert _only(outcomes).audit_id is None
    session.expire_all()
    assert session.query(Visit).count() == 1  # still visible
    assert session.query(ChartAuditEvent).count() == 0  # and unaudited


def test_history_is_patient_scoped_and_newest_first(chart):
    session, patient, _ = chart
    condition = session.query(Condition).first()
    stamps = [datetime(2026, 6, 1, 9, 0), datetime(2026, 6, 2, 9, 0)]
    for index, reason in enumerate(("first change", "second change")):
        apply_edits(
            session,
            ACTOR,
            [ChartEdit("Condition", condition.id, EditAction.AMEND, {"chronic": index == 0}, reason)],
            now=stamps[index],
        )
    trail = history(session, patient.id)
    assert [event.reason for event in trail] == ["second change", "first change"]
    assert history(session, patient.id + 999) == []


def test_purge_visit_really_deletes_and_is_admin_only(chart):
    session, patient, visit = chart
    apply_edits(session, ACTOR, [ChartEdit("Visit", visit.id, EditAction.VOID, reason="duplicate")])
    counts = purge_visit(session, visit.id)
    assert counts["Visit"] == 1 and counts["Condition"] == 1

    from sqlalchemy import select

    remaining = session.execute(
        select(Visit).where(Visit.id == visit.id).execution_options(include_voided=True)
    ).scalar_one_or_none()
    assert remaining is None
    assert session.query(Allergy).count() == 1  # patient-owned rows survive
    with pytest.raises(ValueError):
        purge_visit(session, visit.id)


# ── the comprehension applier's writes are audited too (§7 Q2) ────────


def test_pipeline_writes_land_in_the_same_trail(chart):
    from hdh.core.chartedit import record_creation
    from hdh.core.chartedit.contracts import Actor as ActorType

    session, patient, visit = chart
    row = Condition(
        patient_id=patient.id,
        visit_id=visit.id,
        icd10_code="R51.9",
        description="Headache",
        status=ConditionStatus.ACTIVE,
    )
    session.add(row)
    session.flush()
    audit_id = record_creation(
        session,
        ActorType(name="Dr. Priya Sharma, MD", source=EditSource.PIPELINE, provider_id=None),
        "Condition",
        row,
        reason="charted from note",
    )
    session.commit()
    event = session.get(ChartAuditEvent, audit_id)
    assert event.action.value == "create" and event.actor_source is EditSource.PIPELINE
    assert event.entity == "Condition" and event.patient_id == patient.id
