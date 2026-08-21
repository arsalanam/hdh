"""Service requests: the chart can finally record what was ASKED FOR.

Milestone A of docs/design/service-requests-and-interchange.md. The tests
that matter here are not "a row round-trips" but the contracts the design
argues for: an uncoded request is legitimate state, provenance cannot be
rewritten, one order can be fulfilled many times, and every change reaches
the audit trail through the single sanctioned path.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.models import (
    LabResult,
    LabStatus,
    Patient,
    Prescription,
    RequestOrigin,
    RequestStatus,
    ServiceKind,
    ServiceRequest,
    Sex,
    Visit,
    VisitType,
)


@pytest.fixture
def chart(db_session, request):
    """One patient with one visit — the smallest chart an order needs.

    `db_session` is session-scoped and shared, so each test gets its OWN
    patient: a fixed MRN would collide on the second test, and worse, one
    test's committed orders would show up in another's counts.
    """
    patient = Patient(
        mrn=f"MRN-{request.node.name[:40]}",
        first_name="Ada",
        last_name="Byron",
        date_of_birth=date(1975, 3, 2),
        sex=Sex.FEMALE,
    )
    db_session.add(patient)
    db_session.flush()
    visit = Visit(
        patient_id=patient.id,
        visit_date=date(2026, 5, 4),
        visit_type=VisitType.FOLLOW_UP,
    )
    db_session.add(visit)
    db_session.flush()
    return patient, visit


def _order(patient, visit, **overrides) -> ServiceRequest:
    fields = {
        "patient_id": patient.id,
        "visit_id": visit.id,
        "kind": ServiceKind.LAB,
        "status": RequestStatus.DRAFT,
        "origin": RequestOrigin.CLINICIAN,
        "display": "Basic metabolic panel",
        "requested_date": date(2026, 5, 4),
    }
    fields.update(overrides)
    return ServiceRequest(**fields)


def test_a_request_is_real_before_it_is_coded(db_session, chart):
    """The point of ordering something is that it exists before anyone has
    resolved a code for it. An uncoded request is legitimate state, not an
    error — refuse-don't-guess means we never invent a code to fill a
    column (design §2)."""
    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.commit()

    stored = db_session.get(ServiceRequest, request.id)
    assert stored.code is None and stored.code_system is None
    assert stored.display == "Basic metabolic panel"
    assert stored.status is RequestStatus.DRAFT


def test_one_order_can_be_fulfilled_many_times(db_session, chart):
    """A basic metabolic panel returns eight results and a prescription is
    dispensed again at every refill, so the foreign key has to sit on the
    fulfilment side. §4 draws `fulfilled_by` from the request, which is the
    reading direction, not the cardinality."""
    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.flush()

    for name, value in (("Sodium", 139.0), ("Potassium", 4.1), ("Creatinine", 0.9)):
        db_session.add(
            LabResult(
                visit_id=visit.id,
                request_id=request.id,
                test_name=name,
                value=value,
                status=LabStatus.NORMAL,
            )
        )
    db_session.commit()

    stored = db_session.get(ServiceRequest, request.id)
    assert len(stored.lab_results) == 3
    assert {r.test_name for r in stored.fulfilled_by} == {"Sodium", "Potassium", "Creatinine"}
    assert stored.lab_results[0].request is stored


def test_medication_order_and_prescription_stay_separate(db_session, chart):
    """Order and dispense are different events. Merging them would make
    "prescribed but never filled" unrepresentable (design §9 Q2) — so an
    order with no prescription must be a perfectly ordinary state."""
    patient, visit = chart
    ordered = _order(patient, visit, kind=ServiceKind.MEDICATION, display="Lisinopril 10 mg")
    never_filled = _order(patient, visit, kind=ServiceKind.MEDICATION, display="Atorvastatin 20 mg")
    db_session.add_all([ordered, never_filled])
    db_session.flush()
    db_session.add(Prescription(visit_id=visit.id, request_id=ordered.id, drug_name="Lisinopril"))
    db_session.commit()

    assert len(db_session.get(ServiceRequest, ordered.id).prescriptions) == 1
    assert db_session.get(ServiceRequest, never_filled.id).prescriptions == []


def test_a_lab_result_need_not_be_a_number(db_session, chart):
    """The most consequential omission OMOP surfaced: `value` was a float,
    so "no growth", "positive" and "<0.01" were unstorable (design §3)."""
    patient, visit = chart
    db_session.add_all(
        [
            LabResult(
                visit_id=visit.id,
                test_name="Urine culture",
                value=None,
                value_text="no growth",
                status=LabStatus.NORMAL,
            ),
            LabResult(
                visit_id=visit.id,
                test_name="Troponin I",
                value=0.01,
                comparator="<",
                unit="ng/mL",
                status=LabStatus.NORMAL,
            ),
        ]
    )
    db_session.commit()

    qualitative = db_session.query(LabResult).filter_by(test_name="Urine culture").one()
    assert qualitative.value is None and qualitative.value_text == "no growth"
    censored = db_session.query(LabResult).filter_by(test_name="Troponin I").one()
    assert (censored.comparator, censored.value) == ("<", 0.01)


# ── chartedit integration ────────────────────────────────────────────────


def _actor():
    from hdh.core.chartedit import Actor
    from hdh.core.models import EditSource

    return Actor(name="tester", source=EditSource.CLI)


def test_provenance_and_request_date_cannot_be_rewritten(db_session, chart):
    """`origin` and `requested_date` are what make the row auditable. A
    chart that can rewrite them cannot answer "who ordered this, and
    when", so they are absent from the amendable set on purpose."""
    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.commit()

    for field, value in (("origin", "external"), ("requested_date", "2020-01-01")):
        outcome = apply_edits(
            db_session,
            _actor(),
            [ChartEdit("ServiceRequest", request.id, EditAction.AMEND, {field: value}, "trying it on")],
        )[0]
        assert not outcome.applied, f"{field} must not be amendable"
        assert field in outcome.detail

    db_session.expunge_all()
    stored = db_session.get(ServiceRequest, request.id)
    assert stored.origin is RequestOrigin.CLINICIAN
    assert stored.requested_date == date(2026, 5, 4)


def test_coding_an_order_later_is_an_audited_amendment(db_session, chart):
    """`code` starts NULL and a coder fills it in — through the one
    sanctioned path, so the trail shows the code did not come from the
    clinician who placed the order."""
    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits, history

    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.commit()

    outcome = apply_edits(
        db_session,
        _actor(),
        [
            ChartEdit(
                "ServiceRequest",
                request.id,
                EditAction.AMEND,
                {"code": "51990-0", "code_system": "loinc"},
                "coded by the LOINC module",
            )
        ],
    )[0]
    assert outcome.applied

    db_session.expunge_all()
    assert db_session.get(ServiceRequest, request.id).code == "51990-0"
    events = history(db_session, patient.id)
    assert any(e.entity == "ServiceRequest" and e.after.get("code") == "51990-0" for e in events)


def test_a_voided_order_stops_being_visible(db_session, chart):
    """Void, never delete: the row stays so its audit event keeps a
    referent, but ordinary reads no longer see it."""
    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.commit()

    applied = apply_edits(
        db_session,
        _actor(),
        [ChartEdit("ServiceRequest", request.id, EditAction.VOID, {}, "ordered in error")],
    )[0]
    assert applied.applied

    # session.get answers from the identity map, so a fresh look is needed
    # to exercise the loader criterion (documented in chartedit/visibility)
    db_session.expunge_all()
    assert db_session.query(ServiceRequest).filter_by(id=request.id).one_or_none() is None
    still_there = (
        db_session.query(ServiceRequest).filter_by(id=request.id).execution_options(include_voided=True).one()
    )
    assert still_there.voided_at is not None


def test_an_enum_label_reads_as_a_person_wrote_it(db_session, chart):
    """Outcome lines are read by humans: "lab", not "ServiceKind.LAB"."""
    from hdh.core.chartedit import spec_for

    patient, visit = chart
    request = _order(patient, visit)
    db_session.add(request)
    db_session.flush()

    described = spec_for("ServiceRequest").describe(request)
    assert "lab · Basic metabolic panel" in described
    assert "ServiceKind" not in described


# ── follow-ups are orders now (issue #59, design §9 Q5) ──────────────────


def test_follow_up_days_is_read_from_the_order(db_session, chart):
    """The scalar became a derived read. Two writable copies of one fact
    drift silently, so the request is the single source of truth."""
    from datetime import timedelta

    patient, visit = chart
    assert visit.follow_up_days is None  # PRN until something asks

    db_session.add(
        _order(
            patient,
            visit,
            kind=ServiceKind.FOLLOW_UP,
            display="Follow-up visit in 90 days",
            occurrence_date=visit.visit_date + timedelta(days=90),
        )
    )
    db_session.commit()
    db_session.expunge_all()

    stored = db_session.get(Visit, visit.id)
    assert stored.follow_up_days == 90
    assert stored.follow_up_request.kind is ServiceKind.FOLLOW_UP


def test_the_scalar_can_no_longer_be_written(db_session, chart):
    """A setter would reopen the dual-write the design rejected."""
    patient, visit = chart
    with pytest.raises(AttributeError):
        visit.follow_up_days = 30


def test_voiding_the_order_removes_the_follow_up(db_session, chart):
    """Cancelling a return visit is a chart edit like any other, and the
    derived value has to follow it — otherwise the note still says "follow
    up in 90 days" after a clinician cancelled it."""
    from datetime import timedelta

    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

    patient, visit = chart
    request = _order(
        patient,
        visit,
        kind=ServiceKind.FOLLOW_UP,
        display="Follow-up visit in 60 days",
        occurrence_date=visit.visit_date + timedelta(days=60),
    )
    db_session.add(request)
    db_session.commit()
    assert db_session.get(Visit, visit.id).follow_up_days == 60

    apply_edits(
        db_session,
        _actor(),
        [ChartEdit("ServiceRequest", request.id, EditAction.VOID, {}, "patient declined")],
    )
    db_session.expunge_all()
    assert db_session.get(Visit, visit.id).follow_up_days is None


def test_amending_the_order_moves_the_return_visit(db_session, chart):
    """The practical payoff of #59: a return visit is now amendable and
    audited, instead of an integer nobody can explain the provenance of."""
    from datetime import timedelta

    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

    patient, visit = chart
    request = _order(
        patient,
        visit,
        kind=ServiceKind.FOLLOW_UP,
        display="Follow-up visit in 90 days",
        occurrence_date=visit.visit_date + timedelta(days=90),
    )
    db_session.add(request)
    db_session.commit()

    moved = (visit.visit_date + timedelta(days=30)).isoformat()
    outcome = apply_edits(
        db_session,
        _actor(),
        [
            ChartEdit(
                "ServiceRequest",
                request.id,
                EditAction.AMEND,
                {"occurrence_date": moved},
                "patient asked to be seen sooner",
            )
        ],
    )[0]
    assert outcome.applied

    db_session.expunge_all()
    assert db_session.get(Visit, visit.id).follow_up_days == 30
