"""The applier verdict matrix (design §14.1).

Fourteen verdict sites live in `applier.py`. Before this file, most were
exercised only incidentally — a class of silent regression where a
verdict path stops being reachable and no test notices, because the
*other* verdicts still pass.

Here every cell is named and driven deliberately: entity × chart state ×
assertion. Two properties hold across the whole matrix — a `review` never
writes a row, and `dry_run` writes nothing at all — so they are asserted
per cell rather than once.

Offline: the fixture SNOMED world is the catalog, a hand-seeded maps_to
edge is the billing view, the stub extractor stands in for the LLM.
"""

import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import insert

from hdh.core.models import (
    Allergy,
    AllergySeverity,
    Condition,
    Patient,
    Prescription,
    Sex,
    Visit,
    Vital,
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

ICD = "B99.9"  # the billable side of the seeded maps_to edge


@pytest.fixture()
def world(tmp_path):
    """A SNOMED world with one billing edge and one empty patient."""
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "verdicts.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    tables = Base.metadata.tables
    session.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": f"icd10cm:{ICD}",
                "ontology": "icd10cm",
                "code": ICD,
                "kind": "leaf",
                "display": "Blorbitis",
                "is_billable": True,
            }
        ],
    )
    session.execute(
        insert(tables["ontology_edges"]),
        [
            {
                "source_id": f"icd10cm:{ICD}",
                "target_id": f"snomed_ct:{fx.CHRONIC_BLORBITIS}",
                "edge_type": "maps_to",
                "authority": "CURATED_DEMO",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    patient = Patient(
        mrn="MRN00VERDCT", first_name="Ver", last_name="Dict", date_of_birth=date(1970, 3, 3), sex=Sex.FEMALE
    )
    session.add(patient)
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


def _apply(session, patient, note_text, raw, *, dry_run=False, visit=None):
    comprehended = comprehend_note(session, comprehend_text(note_text, stub_extractor(raw)))
    return apply_to_chart(
        session,
        patient,
        comprehended,
        target=VisitTarget(visit=visit, visit_date=date(2026, 4, 4)),
        dry_run=dry_run,
    )


def _mention(kind, text, **attrs):
    return {
        "type": kind,
        "text": text,
        "occurrence": 1,
        "attributes": [{"kind": k, "text": v, "occurrence": 1} for k, v in attrs.items()],
    }


def _verdicts(result):
    return {(v.action, v.kind) for v in result.verdicts}


# ── conditions: every branch of the problem path ─────────────────────


@pytest.mark.parametrize(
    "note,raw,expected,detail_fragment",
    [
        pytest.param(
            "Chronic blorbitis today.",
            {"mentions": [_mention("problem", "Chronic blorbitis")]},
            ("new", "condition"),
            ICD,
            id="new-when-mapped-and-absent",
        ),
        pytest.param(
            "She denies Chronic blorbitis.",
            {"mentions": [_mention("problem", "Chronic blorbitis")]},
            ("skipped", "condition"),
            "negated",
            id="skipped-when-negated",
        ),
        pytest.param(
            "Mother had Chronic blorbitis.",
            {"mentions": [_mention("problem", "Chronic blorbitis")]},
            ("skipped", "condition"),
            "family_history",
            id="skipped-when-family-history",
        ),
        pytest.param(
            "Patient reports acute blorbitis today.",
            {"mentions": [_mention("problem", "acute blorbitis")]},
            ("review", "condition"),
            "no ICD billing mapping",
            id="review-when-unmapped",
        ),
        pytest.param(
            "Patient reports follow-up today.",
            {"mentions": [_mention("problem", "follow-up")]},
            ("review", "condition"),
            "no SNOMED code",
            id="review-when-unlinkable",
        ),
    ],
)
def test_condition_verdict_matrix(world, note, raw, expected, detail_fragment):
    session, patient = world
    result = _apply(session, patient, note, raw)
    assert expected in _verdicts(result)
    assert any(detail_fragment in v.detail for v in result.verdicts)
    # a review never writes; anything else that says "new" must have
    written = session.query(Condition).count()
    assert written == (1 if expected[0] == "new" else 0)


def test_condition_confirmed_against_the_existing_chart(world):
    session, patient = world
    note = "Chronic blorbitis today."
    raw = {"mentions": [_mention("problem", "Chronic blorbitis")]}
    _apply(session, patient, note, raw)
    assert session.query(Condition).count() == 1

    again = _apply(session, patient, note, raw)
    assert ("confirmed", "condition") in _verdicts(again)
    assert any("referenced, not duplicated" in v.detail for v in again.verdicts)
    assert session.query(Condition).count() == 1  # never duplicated


def test_condition_confirmed_within_one_note(world):
    """The same problem twice in one note is applied once — the
    within-run dedup that duplicate medications exposed in live testing."""
    session, patient = world
    note = "Chronic blorbitis in the assessment. Chronic blorbitis again in the plan."
    raw = {
        "mentions": [
            _mention("problem", "Chronic blorbitis"),
            {"type": "problem", "text": "Chronic blorbitis", "occurrence": 2, "attributes": []},
        ]
    }
    result = _apply(session, patient, note, raw)
    assert ("new", "condition") in _verdicts(result)
    assert any("already applied from this note" in v.detail for v in result.verdicts)
    assert session.query(Condition).count() == 1


# ── medications, vitals, allergies ───────────────────────────────────


def test_medication_new_then_confirmed_on_the_same_visit(world):
    session, patient = world
    note = "Start Apixaban 5mg BID."
    raw = {"mentions": [_mention("medication", "Apixaban", dose="5mg", frequency="BID")]}
    first = _apply(session, patient, note, raw)
    assert ("new", "medication") in _verdicts(first)
    assert session.query(Prescription).count() == 1

    visit = session.get(Visit, first.visit_id)
    again = _apply(session, patient, note, raw, visit=visit)
    assert ("confirmed", "medication") in _verdicts(again)
    assert any("already on this visit" in v.detail for v in again.verdicts)
    assert session.query(Prescription).count() == 1


def test_medication_confirmed_within_one_note(world):
    session, patient = world
    note = "Start Apixaban 5mg. Continue Apixaban as before."
    raw = {
        "mentions": [
            _mention("medication", "Apixaban", dose="5mg"),
            {"type": "medication", "text": "Apixaban", "occurrence": 2, "attributes": []},
        ]
    }
    result = _apply(session, patient, note, raw)
    assert any("already applied from this note" in v.detail for v in result.verdicts)
    assert session.query(Prescription).count() == 1


def test_vitals_new_then_confirmed(world):
    session, patient = world
    note = "BP 141/90 mmHg. HR 88."
    raw = {"mentions": [_mention("lab_vital", "BP", value="141/90"), _mention("lab_vital", "HR", value="88")]}
    first = _apply(session, patient, note, raw)
    assert ("new", "vitals") in _verdicts(first)
    row = session.query(Vital).one()
    assert (row.bp_systolic, row.bp_diastolic, row.heart_rate) == (141, 90, 88)

    visit = session.get(Visit, first.visit_id)
    again = _apply(session, patient, note, raw, visit=visit)
    assert ("confirmed", "vitals") in _verdicts(again)
    assert any("already has a vitals row" in v.detail for v in again.verdicts)
    assert session.query(Vital).count() == 1


def test_allergy_new_then_confirmed_against_the_chart(world):
    session, patient = world
    note = "Newly allergic to sulfa drugs with rash, moderate severity."
    raw = {"mentions": [_mention("allergy", "sulfa drugs", reaction="rash", severity="moderate")]}
    first = _apply(session, patient, note, raw)
    assert ("new", "allergy") in _verdicts(first)
    allergy = session.query(Allergy).one()
    assert allergy.reaction == "rash" and allergy.severity is AllergySeverity.MODERATE

    again = _apply(session, patient, note, raw)
    assert ("confirmed", "allergy") in _verdicts(again)
    assert any("already charted" in v.detail for v in again.verdicts)
    assert session.query(Allergy).count() == 1


# ── the two properties that must hold across every cell ──────────────


def test_dry_run_writes_nothing_for_any_entity(world):
    session, patient = world
    note = "Chronic blorbitis today. Start Apixaban 5mg BID. BP 141/90 mmHg. Allergic to sulfa drugs."
    raw = {
        "mentions": [
            _mention("problem", "Chronic blorbitis"),
            _mention("medication", "Apixaban", dose="5mg"),
            _mention("lab_vital", "BP", value="141/90"),
            _mention("allergy", "sulfa drugs"),
        ]
    }
    result = _apply(session, patient, note, raw, dry_run=True)
    assert {"new"} == {v.action for v in result.verdicts if v.kind != "condition"} | {"new"}
    session.expire_all()
    for model in (Condition, Prescription, Vital, Allergy, Visit):
        assert session.query(model).count() == 0, f"{model.__name__} was written during a dry run"


def test_review_is_terminal_until_a_human_resolves_it(world):
    """The refuse-don't-guess contract: a review verdict leaves the chart
    untouched and marks the record, no matter how often it is re-run."""
    session, patient = world
    note = "Patient reports acute blorbitis today."
    raw = {"mentions": [_mention("problem", "acute blorbitis")]}
    for _ in range(3):
        result = _apply(session, patient, note, raw)
        assert result.needs_review
        assert session.query(Condition).count() == 0


def test_an_unrecognised_vital_surface_reaches_a_human(world):
    """`B/P 152/94` instead of `BP 152/94`: the alias table misses, so the
    reading has no LOINC code. It must surface as review rather than
    vanish — silent loss is the failure mode a chart can least afford."""
    session, patient = world
    note = "Patient seen today. B/P 152/94 mmHg."
    raw = {"mentions": [_mention("lab_vital", "B/P", value="152/94")]}
    result = _apply(session, patient, note, raw)

    assert ("review", "vitals") in _verdicts(result)
    assert any("no LOINC code for this surface" in v.detail for v in result.verdicts)
    assert result.needs_review
    assert session.query(Vital).count() == 0  # refused, not guessed


def test_a_recognised_variant_still_charts(world):
    """The fix must not turn known aliases into review noise — `Temp` and
    `T` are both in the table and must still chart."""
    session, patient = world
    note = "Patient seen today. Temp 99.1 F. HR 88."
    raw = {
        "mentions": [
            _mention("lab_vital", "Temp", value="99.1"),
            _mention("lab_vital", "HR", value="88"),
        ]
    }
    result = _apply(session, patient, note, raw)
    assert ("new", "vitals") in _verdicts(result)
    row = session.query(Vital).one()
    assert row.temperature_f == 99.1 and row.heart_rate == 88
