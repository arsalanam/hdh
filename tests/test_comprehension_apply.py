"""Comprehension milestone C: the closed loop (design §10).

Note → comprehended record → chart applier (reconciliation verdicts) →
FHIR document bundle → SOAP round-trip. Offline: the fixture SNOMED
world is the catalog, a hand-seeded maps_to edge supplies the billing
view, stubs stand in for the LLM."""

import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import insert

from hdh.core.models import Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
from hdh.modules.comprehension.assemble import assemble_bundle
from hdh.modules.comprehension.comprehend import comprehend_text
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.comprehension.pipeline import comprehend_note

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

NOTE = (
    "2026-08-15 encounter. Chronic blorbitis remains active. "
    "Newly allergic to sulfa drugs with rash, moderate severity. "
    "Mother had glimmerpox. She denies acute blorbitis. "
    "BP 128/79 mmHg. HR 72. Start Apixaban 5mg BID for chronic blorbitis."
)

RAW = {
    "mentions": [
        {"type": "problem", "text": "Chronic blorbitis", "occurrence": 1, "attributes": []},
        {
            "type": "allergy",
            "text": "sulfa drugs",
            "occurrence": 1,
            "attributes": [
                {"kind": "reaction", "text": "rash", "occurrence": 1},
                {"kind": "severity", "text": "moderate", "occurrence": 1},
            ],
        },
        {"type": "problem", "text": "glimmerpox", "occurrence": 1, "attributes": []},
        {"type": "problem", "text": "acute blorbitis", "occurrence": 1, "attributes": []},
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
            "type": "lab_vital",
            "text": "HR",
            "occurrence": 1,
            "attributes": [
                {"kind": "value", "text": "72", "occurrence": 1},
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
    ],
    "relations": [{"kind": "treats", "source": 6, "target": 0, "inferred": False}],
    "shared_triggers": [],
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Fixture catalog + a patient + the billing maps_to edge."""
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("apply") / "apply.db"))
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
        mrn="MRN00APPLIER",
        first_name="Loop",
        last_name="Closed",
        date_of_birth=date(1959, 3, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def comprehended(world):
    session, _patient = world
    return comprehend_note(session, comprehend_text(NOTE, stub_extractor(RAW)))


def test_apply_creates_visit_and_chart_rows(world, comprehended):
    session, patient = world
    result = apply_to_chart(session, patient, comprehended)
    assert result.created_visit
    actions = {(v.action, v.kind) for v in result.verdicts}
    assert ("new", "condition") in actions
    assert ("new", "medication") in actions
    assert ("new", "vitals") in actions
    assert ("new", "allergy") in actions
    # negated + family-history problems were skipped, never charted
    skipped = [v.detail for v in result.verdicts if v.action == "skipped"]
    assert any("acute blorbitis" in d for d in skipped)
    assert any("glimmerpox" in d for d in skipped)

    session.expire_all()
    conditions = [c for c in patient.conditions]
    assert len(conditions) == 1
    assert conditions[0].icd10_code == "B99.8"  # the billing view via maps_to
    assert conditions[0].snomed_code == fx.CHRONIC_BLORBITIS
    visit = conditions[0].visit
    assert visit.vitals.bp_systolic == 128 and visit.vitals.heart_rate == 72
    assert visit.prescriptions[0].drug_name == "Apixaban" and visit.prescriptions[0].is_new
    allergy = patient.allergies[0]
    assert allergy.substance == "sulfa drugs" and allergy.reaction == "rash"
    assert str(allergy.severity).endswith("MODERATE")


def test_second_apply_reconciles_confirmed_no_duplicates(world, comprehended):
    session, patient = world
    visit = patient.conditions[0].visit
    result = apply_to_chart(session, patient, comprehended, target=VisitTarget(visit=visit))
    assert not result.created_visit
    assert not any(v.action == "new" for v in result.verdicts)  # nothing re-created
    assert any(v.action == "confirmed" for v in result.verdicts)  # everything reconciled
    session.expire_all()
    assert len(patient.conditions) == 1 and len(patient.allergies) == 1


def test_unmapped_problem_goes_to_review_not_chart(world):
    session, patient = world
    raw = {
        "mentions": [{"type": "problem", "text": "Blorbitis of flenum", "occurrence": 1, "attributes": []}]
    }
    note = comprehend_note(
        session, comprehend_text("Blorbitis of flenum suspected resolved.", stub_extractor(raw))
    )
    before = len(patient.conditions)
    result = apply_to_chart(session, patient, comprehended_note_override(note))
    review = [v for v in result.verdicts if v.action == "review"]
    assert review and "no ICD billing mapping" in review[0].detail
    session.expire_all()
    assert len(patient.conditions) == before  # review wrote NOTHING


def comprehended_note_override(note):
    """Force a PRESENT assertion for the review-path test (the note text
    above trips 'suspected'/'resolved' triggers by design of the test)."""
    from dataclasses import replace

    from hdh.modules.comprehension.contextualize import AssertionResult
    from hdh.modules.comprehension.contracts import Assertion

    mentions = tuple(
        replace(m, assertion=AssertionResult(Assertion.PRESENT, "test override")) for m in note.mentions
    )
    return replace(note, mentions=mentions)


def test_fhir_document_bundle_shape(world, comprehended):
    session, _patient = world
    bundle = assemble_bundle(session, comprehended, subject_display="Loop Closed")
    assert bundle["type"] == "document"
    assert bundle["entry"][0]["resource"]["resourceType"] == "Composition"
    by_type: dict[str, list] = {}
    for entry in bundle["entry"][1:]:
        by_type.setdefault(entry["resource"]["resourceType"], []).append(entry["resource"])

    condition = by_type["Condition"][0]  # negated/family problems are absent
    systems = {c["system"] for c in condition["code"]["coding"]}
    assert "http://snomed.info/sct" in systems and "http://hl7.org/fhir/sid/icd-10" in systems

    bp = next(o for o in by_type["Observation"] if o["code"]["coding"][0]["code"] == "55284-4")
    component_codes = {c["code"]["coding"][0]["code"] for c in bp["component"]}
    assert component_codes == {"8480-6", "8462-4"}  # the emitter shape, from a note

    rx = by_type["MedicationRequest"][0]
    assert rx["dosageInstruction"][0]["text"] == "5mg BID"
    assert rx["reasonReference"]  # the grounded TREATS relation
    assert any("urn:hdh:mention-span" == e["url"] for e in rx["extension"])  # provenance

    assert by_type["AllergyIntolerance"][0]["reaction"][0]["manifestation"][0]["text"] == "rash"


def test_soap_round_trip_renders_from_applied_chart(world):
    session, patient = world
    from hdh.core.notes import visit_to_soap

    visit = patient.conditions[0].visit
    soap = visit_to_soap(visit, patient)
    assert "Chronic blorbitis" in soap  # the note's diagnosis, back as text
    assert "Apixaban 5mg BID" in soap
    assert "BP 128/79" in soap
    assert "sulfa drugs" in soap.lower() or "sulfa" in soap


def test_dry_run_computes_verdicts_but_writes_nothing(world, comprehended):
    session, patient = world
    before_visits = len(patient.visits)
    before_allergies = len(patient.allergies)
    result = apply_to_chart(session, patient, comprehended, dry_run=True)
    assert result.verdicts  # the full verdict table was computed
    session.expire_all()
    assert len(patient.visits) == before_visits  # ...and nothing persisted
    assert len(patient.allergies) == before_allergies
