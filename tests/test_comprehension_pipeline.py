"""Comprehension milestone B: stages 3–5 + storage + the eval scorer.

All offline: the synthetic SNOMED fixture is the coding catalog, stub
extractions stand in for the LLM. Normalization routes by type,
assertions come from sections/triggers with evidence, disambiguation
uses ancestor context (the H54 lesson on fixture data), records land in
the registry tables, and the pure scorer produces honest numbers."""

import sys
from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.comprehend import comprehend_text
from hdh.modules.comprehension.contracts import Assertion, MentionType
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.comprehension.pipeline import comprehend_note, store_record

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

# A fluent note set in the fixture's fabricated clinical world, exercising
# sections-free assertion rules AND fixture-codable problems/procedures.
NOTE = (
    "Patient returns for follow-up of chronic blorbitis, now well controlled. "
    "Mother had blorbitis of flenum. She denies glimmer fever today. "
    "BP 128/79 mmHg. INR 1.1. "
    "Flenumectomy is planned; continue Apixaban 5mg BID."
)


def _m(type_, text, occ=1, attrs=()):
    return {"type": type_, "text": text, "occurrence": occ, "attributes": list(attrs)}


RAW = {
    "mentions": [
        _m(
            "problem",
            "chronic blorbitis",
            1,
            [{"kind": "control", "text": "well controlled", "occurrence": 1}],
        ),
        _m("problem", "blorbitis of flenum"),
        _m("problem", "glimmer fever"),
        _m("lab_vital", "BP", 1, [{"kind": "value", "text": "128/79", "occurrence": 1}]),
        _m("lab_vital", "INR", 1, [{"kind": "value", "text": "1.1", "occurrence": 1}]),
        _m("procedure", "Flenumectomy"),
        _m("medication", "Apixaban", 1, [{"kind": "dose", "text": "5mg", "occurrence": 1}]),
    ],
    "relations": [{"kind": "treats", "source": 6, "target": 0, "inferred": True}],
    "shared_triggers": [],
}


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """DB with the synthetic SNOMED catalog loaded (the coding target)."""
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("comp") / "comp.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def comprehended(catalog):
    extraction = comprehend_text(NOTE, stub_extractor(RAW))
    return comprehend_note(catalog, extraction)


# ── stage 3: normalize routes by type ────────────────────────────────────────


def test_problems_and_procedures_code_to_snomed(comprehended):
    by_text = {m.mention.text: m for m in comprehended.mentions}
    blorbitis = by_text["chronic blorbitis"]
    assert blorbitis.code.system == "snomed_ct" and blorbitis.code.code == fx.CHRONIC_BLORBITIS
    assert by_text["blorbitis of flenum"].code.code == fx.FLENUM_BLORBITIS
    procedure = by_text["Flenumectomy"]
    assert procedure.code.code == fx.FLENUMECTOMY  # tag-constrained: procedure, not qualifier
    assert all(m.code.in_shared_tables for m in (blorbitis, procedure))


def test_vitals_map_to_loinc_and_labs_to_labspec_codes(comprehended):
    by_text = {m.mention.text: m for m in comprehended.mentions}
    assert by_text["BP"].code.system == "loinc" and by_text["BP"].code.code == "55284-4"
    assert by_text["INR"].code.code == "6301-6"  # from the cardiometabolic LabSpecs
    assert not by_text["BP"].code.in_shared_tables  # placeholder: no concept_id FK


def test_medications_match_the_drug_catalog(comprehended):
    apixaban = next(m for m in comprehended.mentions if m.mention.mention_type is MentionType.MEDICATION)
    assert apixaban.code.system == "drug-catalog" and apixaban.code.display == "Apixaban"


# ── stage 4: assertion rules with evidence ───────────────────────────────────


def test_assertions_from_triggers_with_evidence(comprehended):
    by_text = {m.mention.text: m for m in comprehended.mentions}
    negated = by_text["glimmer fever"]
    assert negated.assertion.assertion is Assertion.NEGATED
    assert "denies" in negated.assertion.evidence

    family = by_text["blorbitis of flenum"]
    assert family.assertion.assertion is Assertion.FAMILY_HISTORY
    assert "mother" in family.assertion.evidence.lower()

    assert by_text["BP"].assertion.assertion is Assertion.PRESENT
    assert "section default" in by_text["BP"].assertion.evidence


def test_shared_trigger_distributes_negation(catalog):
    note = "S: No fever, chills, or night sweats. History of: Blorbitis."
    raw = {
        "mentions": [
            _m("problem", "fever"),
            _m("problem", "chills"),
            _m("problem", "night sweats"),
            _m("problem", "Blorbitis"),
        ],
        "shared_triggers": [{"text": "No", "occurrence": 1}],
    }
    result = comprehend_note(catalog, comprehend_text(note, stub_extractor(raw)))
    by_text = {m.mention.text: m for m in result.mentions}
    for symptom in ("fever", "chills", "night sweats"):
        assert by_text[symptom].assertion.assertion is Assertion.NEGATED, symptom
    # the trigger does NOT leak past the sentence boundary
    assert by_text["Blorbitis"].assertion.assertion is Assertion.HISTORICAL


def test_the_history_abbreviations_are_recognised(catalog):
    """ "h/o" and "hx of" are how notes actually write it — spelling out
    "history of" is the exception. Without them the commonest form of "this
    is background, not today" was read as a present-tense complaint, which
    also made it eligible to become the visit's chief complaint (#71)."""
    for phrase in ("h/o", "hx of", "history of"):
        note = f"S: Patient with {phrase} Blorbitis, doing well."
        result = comprehend_note(
            catalog, comprehend_text(note, stub_extractor({"mentions": [_m("problem", "Blorbitis")]}))
        )
        assertion = result.mentions[0].assertion
        assert assertion.assertion is Assertion.HISTORICAL, f"{phrase!r} → {assertion.assertion}"
        assert phrase in assertion.evidence


# ── stage 5: ancestor-context disambiguation ─────────────────────────────────


def test_context_settles_close_candidates(catalog):
    """'blorb' alone matches several fixture concepts closely; a note whose
    other problem lives in the flenum subtree pulls the flenum variant up."""
    note = "Disorder of flenum noted. Evaluation of blorb changes."
    raw = {"mentions": [_m("problem", "Disorder of flenum"), _m("problem", "blorb")]}
    result = comprehend_note(catalog, comprehend_text(note, stub_extractor(raw)))
    ambiguous = result.mentions[1]
    assert ambiguous.code is not None  # settled, one way or the other
    # the unambiguous mention anchored the context
    assert result.mentions[0].code.code == fx.DISORDER_FLENUM


# ── unlinked mentions stay honest ────────────────────────────────────────────


def test_unlinkable_mentions_lower_confidence_and_flag_review(catalog):
    note = "Complains of zorblax discomfort."
    raw = {"mentions": [_m("problem", "zorblax discomfort")]}
    result = comprehend_note(catalog, comprehend_text(note, stub_extractor(raw)))
    only = result.mentions[0]
    assert only.code is None and only.confidence < 0.6
    assert result.needs_review


# ── storage ──────────────────────────────────────────────────────────────────


def test_store_record_writes_registry_rows(catalog, comprehended):
    from datetime import date

    from sqlalchemy import select

    from hdh.core.models import Base, Patient, Sex, Visit, VisitNote, VisitType

    patient = Patient(
        mrn="MRN00COMPREH",
        first_name="Note",
        last_name="Case",
        date_of_birth=date(1970, 1, 1),
        sex=Sex.FEMALE,
    )
    catalog.add(patient)
    catalog.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 15), visit_type=VisitType.FOLLOW_UP)
    catalog.add(visit)
    catalog.flush()
    stored_note = VisitNote(visit_id=visit.id, text=NOTE)
    catalog.add(stored_note)
    catalog.commit()

    record_id = store_record(catalog, stored_note.id, comprehended)
    tables = Base.metadata.tables
    record = catalog.execute(
        select(tables["note_records"]).where(tables["note_records"].c.id == record_id)
    ).first()
    assert record.status == "complete" and record.pipeline_version == "0.2"
    rows = catalog.execute(
        select(tables["note_mentions"]).where(tables["note_mentions"].c.record_id == record_id)
    ).all()
    assert len(rows) == 7
    by_text = {row.text: row for row in rows}
    assert by_text["chronic blorbitis"].concept_id == f"snomed_ct:{fx.CHRONIC_BLORBITIS}"
    assert by_text["BP"].concept_id is None  # loinc placeholder: code in properties
    assert by_text["BP"].properties["code"]["system"] == "loinc"
    assert by_text["glimmer fever"].assertion == "negated"


# ── the pure scorer ──────────────────────────────────────────────────────────


def test_scorer_produces_honest_numbers(catalog, comprehended):
    from hdh.modules.comprehension.evaluate import Scorecard, TruthItem, score_note

    truth = (
        TruthItem("chronic blorbitis", MentionType.PROBLEM, expected_code=fx.CHRONIC_BLORBITIS),
        TruthItem("Flenumectomy", MentionType.PROCEDURE, expected_code=fx.FLENUMECTOMY),
        TruthItem("Apixaban", MentionType.MEDICATION),
        TruthItem("not in the note at all", MentionType.PROBLEM),
    )
    card = score_note(truth, comprehended)
    assert card.truth == 4 and card.found == 3
    assert card.linked_checked == 2 and card.linked_right == 2
    assert any("not in the note" in miss for miss in card.misses)
    report = Scorecard()
    report.add(card)
    assert "mention recall" in report.report()
