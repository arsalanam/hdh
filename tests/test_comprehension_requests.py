"""Milestone B: the note's plan finally reaches the chart.

The chart could record what HAPPENED and never what was ASKED FOR, so a
plan line was comprehended correctly and then dropped on the floor. These
tests cover the four rows of §5's table and, more importantly, the rules
that keep the pass honest: the section is what separates a result from a
request, an uncoded order is legitimate, and an order for something the
note denies is not an order.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from hdh.core.models import (
    Patient,
    RequestOrigin,
    RequestStatus,
    ServiceKind,
    ServiceRequest,
    Sex,
    Visit,
    VisitType,
    get_engine,
    get_session,
)
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
from hdh.modules.comprehension.comprehend import comprehend_text
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.comprehension.pipeline import comprehend_note

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

VISIT_DATE = date(2026, 5, 4)

# A SOAP note whose PLAN carries all four kinds of order from §5's table.
# "P:" is what the segmenter keys on, and the section is what makes the
# LAB_VITAL in the plan a REQUEST rather than a result.
NOTE = (
    "SOAP NOTE — 2026-05-04  (Dr. Sarah Mitchell, MD)\n"
    "S: 60-year-old female presents with: chronic blorbitis follow-up.\n"
    "O: BP 128/79 mmHg.\n"
    "A: Chronic blorbitis (B99.8).\n"
    "P: Start Apixaban 5mg BID for chronic blorbitis. "
    "Order glimmerpox panel before that visit. "
    "Referral to cardiology. "
    "Follow up in 30 days."
)

RAW = {
    "mentions": [
        {"type": "problem", "text": "Chronic blorbitis", "occurrence": 1, "attributes": []},
        {
            "type": "lab_vital",
            "text": "BP",
            "occurrence": 1,
            "attributes": [
                {"kind": "value", "text": "128/79", "occurrence": 1},
                {"kind": "unit", "text": "mmHg", "occurrence": 1},
            ],
        },
        {
            "type": "medication",
            "text": "Apixaban",
            "occurrence": 1,
            "attributes": [
                {"kind": "dose", "text": "5mg", "occurrence": 1},
                {"kind": "frequency", "text": "BID", "occurrence": 1},
                {"kind": "status_word", "text": "Start", "occurrence": 1},
            ],
        },
        {"type": "lab_vital", "text": "glimmerpox panel", "occurrence": 1, "attributes": []},
    ],
    "relations": [{"kind": "treats", "source": 2, "target": 0, "inferred": False}],
    "shared_triggers": [],
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The fixture SNOMED catalog, a patient, and one visit to order on."""
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("requests") / "requests.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)

    tables = Base.metadata.tables
    session.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": "icd10cm:B99.8",
                "ontology": "icd10cm",
                "code": "B99.8",
                "kind": "code",
                "display": "Other infectious disease",
            }
        ],
    )
    session.execute(
        insert(tables["ontology_edges"]),
        [
            {
                "source_id": "icd10cm:B99.8",
                "target_id": f"snomed_ct:{fx.CHRONIC_BLORBITIS}",
                "edge_type": "maps_to",
                "authority": "CURATED_DEMO",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    patient = Patient(
        mrn="MRN00REQUEST",
        first_name="Plan",
        last_name="Reaches",
        date_of_birth=date(1966, 2, 2),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


@pytest.fixture
def applied(world):
    """Comprehend the note onto a FRESH visit, and hand back the verdicts."""
    session, patient = world
    visit = Visit(
        patient_id=patient.id,
        visit_date=VISIT_DATE,
        visit_type=VisitType.FOLLOW_UP,
        chief_complaint="Chronic blorbitis follow-up",
    )
    session.add(visit)
    session.flush()
    note = comprehend_note(session, comprehend_text(NOTE, stub_extractor(RAW)))
    result = apply_to_chart(session, patient, note, VisitTarget(visit=visit))
    yield session, patient, visit, result
    # leave the chart as we found it, so each test sees one visit's orders
    for request in list(visit.service_requests):
        session.delete(request)
    session.commit()


def _requests(visit) -> dict[ServiceKind, list[ServiceRequest]]:
    grouped: dict[ServiceKind, list[ServiceRequest]] = {}
    for request in visit.service_requests:
        grouped.setdefault(request.kind, []).append(request)
    return grouped


def test_every_kind_in_the_plan_becomes_an_order(applied):
    """§5's table, end to end: a drug, a panel, a referral and a return."""
    _session, _patient, visit, _result = applied
    by_kind = _requests(visit)
    assert set(by_kind) == {
        ServiceKind.MEDICATION,
        ServiceKind.LAB,
        ServiceKind.REFERRAL,
        ServiceKind.FOLLOW_UP,
    }, sorted(k.value for k in by_kind)


def test_a_result_in_the_objective_section_is_not_an_order(applied):
    """The section is the whole safety argument for this pass: BP appears
    as a LAB_VITAL in the objective, and must stay a reading."""
    _session, _patient, visit, _result = applied
    labs = _requests(visit)[ServiceKind.LAB]
    displays = {r.display for r in labs}
    assert displays == {"glimmerpox panel"}, displays
    assert "BP" not in displays


def test_orders_arrive_as_drafts_from_comprehension(applied):
    """DRAFT, not ACTIVE: comprehending an order is not releasing it —
    `hdh orders release` is the human act that sends it. And `origin`
    records that a note put it there, which is the OMOP lesson (§3)."""
    _session, _patient, visit, _result = applied
    for request in visit.service_requests:
        assert request.status is RequestStatus.DRAFT
        assert request.origin is RequestOrigin.COMPREHENSION
        assert request.requester_id == visit.provider_id


def test_a_medication_order_keeps_the_directions_verbatim(applied):
    """`sig` matters most of the OMOP fields: the direction line is what a
    pharmacy actually reads, so it keeps the note's words."""
    _session, _patient, visit, _result = applied
    med = _requests(visit)[ServiceKind.MEDICATION][0]
    assert med.display == "Apixaban"
    assert "Apixaban" in med.sig and "5mg" in med.sig and "BID" in med.sig


def test_the_treats_relation_is_persisted_at_last(applied):
    """ "Apixaban FOR chronic blorbitis" was derived and then discarded
    after the FHIR export. This is where it lands (design §2)."""
    _session, _patient, visit, _result = applied
    med = _requests(visit)[ServiceKind.MEDICATION][0]
    assert med.reason_condition_id is not None
    assert med.reason_condition.description.lower().startswith("chronic blorbitis")


def test_a_lab_is_due_by_the_follow_up(applied):
    """ "before that visit" — the follow-up is what "that visit" means."""
    _session, _patient, visit, _result = applied
    lab = _requests(visit)[ServiceKind.LAB][0]
    assert lab.occurrence_date == VISIT_DATE + timedelta(days=30)


def test_the_return_visit_is_the_follow_up_order(applied):
    """And the derived scalar reads back through it (#59)."""
    _session, _patient, visit, _result = applied
    follow_up = _requests(visit)[ServiceKind.FOLLOW_UP][0]
    assert follow_up.occurrence_date == VISIT_DATE + timedelta(days=30)
    assert visit.follow_up_days == 30


def test_a_referral_records_its_target(applied):
    _session, _patient, visit, _result = applied
    referral = _requests(visit)[ServiceKind.REFERRAL][0]
    assert referral.display.lower() == "cardiology"
    assert referral.detail == {"specialty": "cardiology"}


def test_an_uncoded_order_is_recorded_not_refused(applied):
    """The one place this pass differs from the condition pass. A problem
    with no billing code cannot be charted faithfully, so it goes to
    review — but a request is real BEFORE anyone codes it, and its display
    is verbatim from the note, so nothing is being guessed (§2)."""
    _session, _patient, visit, result = applied
    referral = _requests(visit)[ServiceKind.REFERRAL][0]
    assert referral.code is None
    assert any(v.action == "new" and "referral" in v.detail for v in result.verdicts)


def test_the_orders_are_audited_like_every_other_write(applied):
    """`hdh chart history` must answer "who put this here?" with "the
    note, via the pipeline"."""
    from hdh.core.chartedit import history

    session, patient, visit, _result = applied
    # scoped to THIS visit's orders: the patient's history accumulates
    # across tests, and counting all of it would measure the fixture
    ordered_ids = {request.id for request in visit.service_requests}
    events = history(session, patient.id, limit=200)
    created = {
        event.row_id: event
        for event in events
        if event.entity == "ServiceRequest" and event.action.value == "create"
    }
    assert ordered_ids <= set(created), (ordered_ids, sorted(created))
    assert {created[row_id].actor_source.value for row_id in ordered_ids} == {"pipeline"}


def test_reapplying_the_same_note_does_not_duplicate_orders(applied):
    """Asking twice is not two orders."""
    session, patient, visit, _result = applied
    note = comprehend_note(session, comprehend_text(NOTE, stub_extractor(RAW)))
    again = apply_to_chart(session, patient, note, VisitTarget(visit=visit))

    assert len(visit.service_requests) == 4
    confirmed = [v for v in again.verdicts if v.action == "confirmed" and v.kind == "request"]
    assert len(confirmed) == 4, [v.detail for v in again.verdicts if v.kind == "request"]


def test_a_denied_plan_item_is_not_ordered(world):
    """An order for something the note says the patient does NOT have is
    not an order — the same refuse-don't-guess line the rest of the
    pipeline holds."""
    session, patient = world
    visit = Visit(
        patient_id=patient.id,
        visit_date=date(2026, 6, 1),
        visit_type=VisitType.FOLLOW_UP,
        chief_complaint="Review",
    )
    session.add(visit)
    session.flush()

    note_text = (
        "SOAP NOTE — 2026-06-01  (Dr. Sarah Mitchell, MD)\n"
        "S: Review.\n"
        "O: No findings.\n"
        "A: Stable.\n"
        "P: No Apixaban indicated at this time."
    )
    raw = {
        "mentions": [
            {"type": "medication", "text": "Apixaban", "occurrence": 1, "attributes": []},
        ],
        "relations": [],
        "shared_triggers": [],
    }
    note = comprehend_note(session, comprehend_text(note_text, stub_extractor(raw)))
    result = apply_to_chart(session, patient, note, VisitTarget(visit=visit))

    assert not [r for r in visit.service_requests if r.kind is ServiceKind.MEDICATION]
    assert any(v.action == "skipped" and v.kind == "request" for v in result.verdicts), [
        (v.action, v.kind, v.detail) for v in result.verdicts
    ]
    for request in list(visit.service_requests):
        session.delete(request)
    session.commit()


def test_a_referral_is_one_order_not_two(world):
    """ "Refer to ophthalmology" reached the chart twice: a PROCEDURE from
    the mention pass and a REFERRAL from the plan-text regex.

    Two open orders for one act — different things to fulfil, to bill, and
    to close a care gap against, so one of them is never satisfied.
    Deduping cannot catch it: `_existing_request` matches within a kind and
    these are two kinds. The referral pass runs last and knows the verb was
    "refer", so it converts the row instead of sitting beside it.
    """
    session, patient = world
    visit = Visit(
        patient_id=patient.id,
        visit_date=date(2026, 6, 8),
        visit_type=VisitType.FOLLOW_UP,
        chief_complaint="Review",
    )
    session.add(visit)
    session.flush()

    note_text = (
        "SOAP NOTE — 2026-06-08  (Dr. Sarah Mitchell, MD)\n"
        "S: Review.\n"
        "O: Nil.\n"
        "A: Stable.\n"
        "P: Refer to ophthalmology."
    )
    raw = {
        "mentions": [
            {"type": "procedure", "text": "ophthalmology", "occurrence": 1, "attributes": []},
        ],
        "relations": [],
        "shared_triggers": [],
    }
    note = comprehend_note(session, comprehend_text(note_text, stub_extractor(raw)))
    result = apply_to_chart(session, patient, note, VisitTarget(visit=visit))
    session.expire_all()

    by_kind = _requests(visit)
    assert len(by_kind.get(ServiceKind.REFERRAL, [])) == 1
    assert not by_kind.get(ServiceKind.PROCEDURE), "the procedure request should have become the referral"
    assert by_kind[ServiceKind.REFERRAL][0].detail == {"specialty": "ophthalmology"}

    # and the verdicts say one thing happened, not two
    request_verdicts = [v.detail for v in result.verdicts if v.kind == "request"]
    assert "referral: ophthalmology" in request_verdicts
    assert "procedure: ophthalmology" not in request_verdicts


def test_a_procedure_that_is_not_a_referral_is_left_alone(world):
    """The conversion must key on the referral verb, not on the word being
    a specialty — an ordered procedure is still a procedure."""
    session, patient = world
    visit = Visit(
        patient_id=patient.id,
        visit_date=date(2026, 6, 9),
        visit_type=VisitType.FOLLOW_UP,
        chief_complaint="Review",
    )
    session.add(visit)
    session.flush()

    note_text = (
        "SOAP NOTE — 2026-06-09  (Dr. Sarah Mitchell, MD)\n"
        "S: Review.\n"
        "O: Nil.\n"
        "A: Stable.\n"
        "P: Order spirometry."
    )
    raw = {
        "mentions": [{"type": "procedure", "text": "spirometry", "occurrence": 1, "attributes": []}],
        "relations": [],
        "shared_triggers": [],
    }
    note = comprehend_note(session, comprehend_text(note_text, stub_extractor(raw)))
    apply_to_chart(session, patient, note, VisitTarget(visit=visit))
    session.expire_all()

    by_kind = _requests(visit)
    assert len(by_kind.get(ServiceKind.PROCEDURE, [])) == 1
    assert not by_kind.get(ServiceKind.REFERRAL)
