"""Milestone C: the round trip closes without a real integration.

Sending an order is easy, and the tests that matter here are all about the
return leg. A result that cannot be matched to an OPEN order is the thing
a naive importer files anyway, so most of this suite is about refusals —
plus the §9 Q4 rider, which says a CKD-4 patient's creatinine must not
come back normal.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hdh.core.models import (
    Condition,
    ConditionStatus,
    LabResult,
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
from hdh.modules.interchange.bundles import (
    order_bundle,
    read_order_bundle,
    read_result_bundle,
    result_bundle,
    write_bundle,
)
from hdh.modules.interchange.contracts import OutboundOrder, PartnerAdapter
from hdh.modules.interchange.importer import import_results, rejected_table
from hdh.modules.interchange.partners import MockLabPartner, MockPharmacyPartner, build_partners

VISIT_DATE = date(2026, 8, 21)


@pytest.fixture(scope="module")
def db_session(tmp_path_factory):
    """Our OWN database, deliberately overriding the shared conftest one.

    These tests add patients and commit, and the shared fixture is what
    other suites count rows in — `test_modules` asserts exactly eight
    patients. A test that changes the world other tests measure is a test
    that will fail somebody else's assertion, eventually and confusingly.
    """
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("interchange") / "ic.db"))
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def chart(db_session, request):
    """One patient, one visit. Own MRN per test: db_session is shared."""
    patient = Patient(
        mrn=f"MRN-IC-{request.node.name[:32]}",
        first_name="Round",
        last_name="Trip",
        date_of_birth=date(1950, 1, 1),
        sex=Sex.FEMALE,
    )
    db_session.add(patient)
    db_session.flush()
    visit = Visit(patient_id=patient.id, visit_date=VISIT_DATE, visit_type=VisitType.FOLLOW_UP)
    db_session.add(visit)
    db_session.flush()
    return patient, visit


def _diagnose(session, patient, visit, code: str, description: str):
    session.add(
        Condition(
            patient_id=patient.id,
            visit_id=visit.id,
            icd10_code=code,
            description=description,
            chronic=True,
            status=ConditionStatus.ACTIVE,
            onset_date=date(2024, 1, 1),
        )
    )
    session.flush()


def _order(session, patient, visit, display: str, **overrides) -> ServiceRequest:
    fields = {
        "patient_id": patient.id,
        "visit_id": visit.id,
        "kind": ServiceKind.LAB,
        "status": RequestStatus.ACTIVE,
        "origin": RequestOrigin.CLINICIAN,
        "display": display,
        "requested_date": VISIT_DATE,
    }
    fields.update(overrides)
    row = ServiceRequest(**fields)
    session.add(row)
    session.flush()
    return row


def _outbound(request: ServiceRequest, mrn: str, diagnoses=()) -> OutboundOrder:
    return OutboundOrder(
        request_id=request.id,
        kind=request.kind.value,
        display=request.display,
        patient_mrn=mrn,
        requested_date=request.requested_date,
        sig=request.sig,
        diagnoses=tuple(diagnoses),
    )


def _deliver(session, inbox, partner, orders, name="result.json"):
    """Run a partner over orders and drop the bundle in the inbox."""
    results = [item for order in orders for item in partner.fulfil(order)]
    write_bundle(inbox, name, result_bundle(partner.name, results))
    return results


# ── the wire ─────────────────────────────────────────────────────────────


def test_an_order_survives_the_round_trip_through_the_bundle(chart):
    """What the partner reads back must be what we sent, diagnoses and all."""
    patient, _visit = chart
    order = OutboundOrder(
        request_id=42,
        kind="lab",
        display="Renal function panel",
        patient_mrn=patient.mrn,
        requested_date=VISIT_DATE,
        occurrence_date=date(2026, 9, 20),
        diagnoses=("N18.4", "I10"),
    )
    restored = read_order_bundle(order_bundle([order]))
    assert restored == [order]


def test_the_mock_partners_satisfy_the_protocol():
    """A real integration replaces one adapter and nothing else (design §6)."""
    for partner in build_partners(seed=1).values():
        assert isinstance(partner, PartnerAdapter)


# ── the §9 Q4 rider ──────────────────────────────────────────────────────


def test_a_ckd_patient_does_not_get_a_healthy_creatinine(db_session, chart):
    """The rider, as an executable check.

    A CKD-4 patient whose kidney labs come back normal produces a chart
    that contradicts itself, which is worse than no lab at all in a dataset
    people learn from. Two things have to be right for this to pass: the
    match is on the ICD-10 CATEGORY (the catalog's profile starts at N18.31
    while this patient has progressed to N18.4), and the SPEC chosen is
    CKD's rather than the annual-physical one that shares the test name.
    """
    patient, visit = chart
    _diagnose(db_session, patient, visit, "N18.4", "Chronic kidney disease, stage 4")
    order = _order(db_session, patient, visit, "Renal function panel")

    results = MockLabPartner().fulfil(_outbound(order, patient.mrn, ["N18.4"]))
    by_name = {item.name: item for item in results}

    assert by_name["eGFR"].abnormal == "low", by_name["eGFR"]
    assert by_name["Creatinine"].abnormal == "high", by_name["Creatinine"]


def test_a_patient_without_the_condition_gets_normal_results(db_session, chart):
    """The other half: condition-awareness must not mean always-abnormal."""
    patient, visit = chart
    order = _order(db_session, patient, visit, "Renal function panel")

    results = MockLabPartner().fulfil(_outbound(order, patient.mrn, diagnoses=[]))
    creatinine = next(item for item in results if item.name == "Creatinine")
    assert creatinine.abnormal == "normal", creatinine


def test_a_diabetic_metabolic_panel_reports_a_high_glucose(db_session, chart):
    """The shift follows the LOINC, not the label.

    The catalog calls the diabetic glucose "Glucose (stat)" while a
    metabolic panel returns "Glucose" — the same measurement (2345-7)
    under two names. Keyed on the name, a type-2 diabetic's panel comes
    back with a perfectly healthy glucose, which is the same class of
    self-contradicting chart the CKD case is about.
    """
    patient, visit = chart
    _diagnose(db_session, patient, visit, "E11.9", "Type 2 diabetes mellitus")
    order = _order(db_session, patient, visit, "Comprehensive metabolic panel")

    results = {item.name: item for item in MockLabPartner().fulfil(_outbound(order, patient.mrn, ["E11.9"]))}

    assert results["Glucose"].abnormal == "high", results["Glucose"]
    # named for what was ORDERED — a lab does not report the catalog's
    # internal variant name back at you
    assert "Glucose (stat)" not in results
    # and diabetes does not make everything abnormal
    assert results["Creatinine"].abnormal == "normal"


def test_a_qualitative_result_crosses_the_wire_as_words(db_session, chart):
    """ "positive" is not a number, and before `value_text` it could not be
    stored at all (design §3)."""
    patient, visit = chart
    _diagnose(db_session, patient, visit, "J10.1", "Influenza")
    order = _order(db_session, patient, visit, "Influenza A/B")

    results = MockLabPartner(rng=__import__("random").Random(2)).fulfil(
        _outbound(order, patient.mrn, ["J10.1"])
    )
    assert results, "the lab could not run a test it has a spec for"
    item = results[0]
    assert item.value is None and item.value_text in ("positive", "negative")

    _partner, restored = read_result_bundle(result_bundle("mock-lab", list(results)))
    assert restored[0].value_text == item.value_text
    assert restored[0].value is None


def test_a_lab_that_cannot_run_the_test_returns_nothing(db_session, chart):
    """A partner says "I can't do that" by returning nothing. Inventing a
    result would be the same sin as guessing a code."""
    patient, visit = chart
    order = _order(db_session, patient, visit, "Whole-body vibe assessment")
    assert MockLabPartner().fulfil(_outbound(order, patient.mrn)) == ()


# ── the return leg ───────────────────────────────────────────────────────


def test_results_land_on_the_chart_and_close_the_order(db_session, chart, tmp_path):
    patient, visit = chart
    _diagnose(db_session, patient, visit, "N18.4", "Chronic kidney disease, stage 4")
    order = _order(db_session, patient, visit, "Renal function panel")
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn, ["N18.4"])])

    outcome = import_results(db_session, inbox)

    assert outcome.filed and not outcome.rejected
    rows = db_session.query(LabResult).filter(LabResult.request_id == order.id).all()
    assert {r.test_name for r in rows} == {"Creatinine", "BUN", "eGFR"}
    assert all(row.visit_id == visit.id for row in rows)
    db_session.refresh(order)
    assert order.status is RequestStatus.COMPLETED


def test_a_dispense_lands_as_a_prescription_linked_to_its_order(db_session, chart, tmp_path):
    patient, visit = chart
    order = _order(
        db_session,
        patient,
        visit,
        "Lisinopril 10 mg",
        kind=ServiceKind.MEDICATION,
        sig="Take one tablet by mouth daily",
    )
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockPharmacyPartner(), [_outbound(order, patient.mrn)])

    outcome = import_results(db_session, inbox)

    assert outcome.filed
    row = db_session.query(Prescription).filter(Prescription.request_id == order.id).one()
    assert row.drug_name == "Lisinopril 10 mg"
    assert row.frequency == "Take one tablet by mouth daily"  # the sig the pharmacy read


def test_an_import_is_audited_as_the_partner(db_session, chart, tmp_path):
    """`hdh chart history` must show a partner's writes beside the
    pipeline's and the agent's."""
    from hdh.core.chartedit import history

    patient, visit = chart
    order = _order(db_session, patient, visit, "Lipid panel")
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)])
    import_results(db_session, inbox)

    events = [
        event
        for event in history(db_session, patient.id, limit=200)
        if event.entity == "LabResult" and event.action.value == "create"
    ]
    assert events, "partner writes never reached the audit trail"
    assert all(event.actor_name == "partner:mock-lab" for event in events)


# ── the refusals: the half that protects the chart ───────────────────────


def _queue(session):
    from sqlalchemy import select

    table = rejected_table()
    return session.execute(select(table).order_by(table.c.id)).all()


def test_a_result_for_an_unknown_order_is_refused(db_session, chart, tmp_path):
    patient, visit = chart
    order = _order(db_session, patient, visit, "Lipid panel")
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)])
    # rewrite the identifier to an order that does not exist
    path = next(inbox.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["entry"]:
        entry["resource"]["basedOn"][0]["identifier"]["value"] = "999999"
    path.write_text(json.dumps(payload), encoding="utf-8")

    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed and outcome.rejected
    assert outcome.needs_review
    added = _queue(db_session)[before:]
    assert {row.reason for row in added} == {"unknown_request"}
    assert db_session.query(LabResult).filter(LabResult.request_id == order.id).count() == 0


def test_a_result_for_an_unreleased_order_is_refused(db_session, chart, tmp_path):
    """Nobody released a DRAFT order, so nothing should be coming back for
    it — and a result that does is worth a human's attention."""
    patient, visit = chart
    draft = _order(db_session, patient, visit, "Lipid panel", status=RequestStatus.DRAFT)
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(draft, patient.mrn)])

    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed
    assert {row.reason for row in _queue(db_session)[before:]} == {"order_not_open"}


def test_a_result_for_a_voided_order_is_refused(db_session, chart, tmp_path):
    """A clinician cancelled the order this morning; the lab did not know."""
    from hdh.core.chartedit import Actor, ChartEdit, EditAction, apply_edits
    from hdh.core.models import EditSource

    patient, visit = chart
    order = _order(db_session, patient, visit, "Lipid panel")
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)])
    apply_edits(
        db_session,
        Actor(name="tester", source=EditSource.CLI),
        [ChartEdit("ServiceRequest", order.id, EditAction.VOID, {}, "ordered in error")],
    )

    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed
    assert {row.reason for row in _queue(db_session)[before:]} == {"order_not_open"}


def test_a_replayed_bundle_does_not_double_the_chart(db_session, chart, tmp_path):
    """Re-sending is normal in real interfaces — a partner retries, a file
    is replayed — and the honest answer is to refuse the second copy rather
    than double the patient's potassium."""
    patient, visit = chart
    order = _order(db_session, patient, visit, "Complete blood count")
    db_session.commit()
    inbox = tmp_path / "inbox"
    order_id = order.id
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)], "first.json")
    import_results(db_session, inbox)
    filed = db_session.query(LabResult).filter(LabResult.request_id == order_id).count()

    # importing completed the order, so a replay would hit order_not_open
    # first; reopen it to isolate the DUPLICATE rule itself
    reopened = db_session.get(ServiceRequest, order_id)
    reopened.status = RequestStatus.ACTIVE
    db_session.commit()
    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed
    assert {row.reason for row in _queue(db_session)[before:]} == {"duplicate_result"}
    assert db_session.query(LabResult).filter(LabResult.request_id == order_id).count() == filed


def test_a_result_for_an_order_with_no_encounter_is_refused(db_session, chart, tmp_path):
    """A result has to live on an encounter. An order placed outside one
    has nowhere to put it, and inventing a visit to hold a lab is not a
    decision an importer should make."""
    patient, visit = chart
    order = _order(db_session, patient, visit, "Lipid panel")
    order.visit_id = None
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)])

    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed
    assert {row.reason for row in _queue(db_session)[before:]} == {"no_encounter"}


def test_an_unreadable_bundle_becomes_a_review_item_not_a_crash(db_session, tmp_path):
    """A result that quotes no order identifier cannot be matched to
    anything, and must not take the import down with it."""
    inbox = tmp_path / "inbox"
    write_bundle(
        inbox,
        "broken.json",
        {
            "resourceType": "Bundle",
            "type": "collection",
            "meta": {"tag": [{"system": "urn:hdh:partner", "code": "mock-lab"}]},
            "entry": [{"resource": {"resourceType": "Observation", "code": {"text": "Sodium"}}}],
        },
    )

    before = len(_queue(db_session))
    outcome = import_results(db_session, inbox)

    assert not outcome.filed and outcome.rejected
    assert {row.reason for row in _queue(db_session)[before:]} == {"unreadable"}


def test_a_dry_run_reports_without_writing(db_session, chart, tmp_path):
    patient, visit = chart
    order = _order(db_session, patient, visit, "Lipid panel")
    db_session.commit()
    inbox = tmp_path / "inbox"
    _deliver(db_session, inbox, MockLabPartner(), [_outbound(order, patient.mrn)])

    order_id = order.id  # read BEFORE the rollback: after it the row is detached
    outcome = import_results(db_session, inbox, dry_run=True)

    assert outcome.filed  # it says what it WOULD do
    db_session.expunge_all()
    assert db_session.query(LabResult).filter(LabResult.request_id == order_id).count() == 0
