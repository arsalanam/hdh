"""FHIR emitter/enricher tests (design fhir-emitters.md §8).

The golden fixture was captured from the PRE-refactor exporter over a
hand-built chart. Ported resource types must reproduce it — normalized
for the volatile fields the old code never kept stable (uuid ids,
timestamps) and for three DELIBERATE changes recorded here:

- Patient.gender: the old ``endswith("MALE")`` check made every FEMALE
  patient "male" (FEMALE ends with MALE) — fixed, so gender is excluded
  from parity and asserted correct separately.
- Condition.clinicalStatus / recordedDate: now the unified problem list's
  real status and onset date, not hardcoded "active" + visit date.
- BP Observation: now a component-based resource (systolic 8480-6 /
  diastolic 8462-4 decimals). The old ``valueQuantity.value = "142/88"``
  string was non-conformant FHIR — caught by the fhir.resources gate —
  so BP observations are excluded from parity and asserted separately.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from hdh.core.fhir import build_bundle, module_enrichers
from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema

GOLDEN = Path(__file__).parent / "fixtures" / "fhir_golden.json"
PORTED_TYPES = {"Patient", "Encounter", "Condition", "Observation", "MedicationRequest"}
DELIBERATE = {
    "Patient": {"gender"},
    "Condition": {"clinicalStatus", "recordedDate"},
}


@pytest.fixture(scope="module")
def golden_patient(tmp_path_factory):
    """The same hand-built chart the golden fixture was captured from."""
    from hdh.core.models import (
        Allergy,
        AllergySeverity,
        Condition,
        ConditionStatus,
        FamilyHistory,
        Immunization,
        LabResult,
        LabStatus,
        MedicationStatement,
        MedicationStatus,
        Patient,
        Prescription,
        Procedure,
        Provider,
        Sex,
        Specialty,
        Visit,
        VisitNote,
        VisitType,
        Vital,
    )

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("fhir") / "golden.db"))
    s = get_session(engine)
    spec = Specialty(code="FM", name="Family Medicine")
    prov = Provider(identifier="NPI1000001", name="Dr. Golden Test, MD", specialty=spec)
    p = Patient(
        mrn="MRN00GOLDEN1"[:12],
        first_name="Golda",
        last_name="Fixture",
        date_of_birth=date(1960, 6, 15),
        sex=Sex.FEMALE,
        race="White",
        ethnicity="Non-Hispanic or Latino",
        address="1 Test Way",
        city="Testville",
        state="TS",
        zip_code="00001",
        phone="555-0100",
        email="golda@example.com",
        insurance_name="Test Mutual",
        insurance_id="TM-1",
        blood_type="O+",
        marital_status="married",
        language="English",
        smoker=False,
        bmi_baseline=27.5,
    )
    s.add_all([spec, prov, p])
    s.flush()
    v = Visit(
        patient_id=p.id,
        visit_date=date(2026, 1, 10),
        visit_type=VisitType.FOLLOW_UP,
        chief_complaint="Diabetes follow-up",
        provider_id=prov.id,
        follow_up_days=90,
    )
    s.add(v)
    s.flush()
    s.add_all(
        [
            Vital(
                visit_id=v.id,
                bp_systolic=142,
                bp_diastolic=88,
                heart_rate=76,
                respiratory_rate=16,
                temperature_f=98.6,
                oxygen_sat=97,
                weight_kg=82.0,
                height_cm=165.0,
                bmi=30.1,
                pain_scale=1,
            ),
            Condition(
                patient_id=p.id,
                visit_id=v.id,
                icd10_code="E11.9",
                description="Type 2 diabetes mellitus without complications",
                chronic=True,
                status=ConditionStatus.ACTIVE,
                controlled=False,
                onset_date=date(2022, 3, 1),
            ),
            Condition(
                patient_id=p.id,
                visit_id=v.id,
                icd10_code="J06.9",
                description="Acute upper respiratory infection, unspecified",
                chronic=False,
                status=ConditionStatus.RESOLVED,
                onset_date=date(2026, 1, 10),
                resolved_date=date(2026, 1, 24),
            ),
            Prescription(
                visit_id=v.id,
                drug_name="Metformin",
                drug_class="Biguanide",
                dose="500 mg",
                frequency="BID",
                duration_days=None,
                refills=3,
                is_new=False,
            ),
            LabResult(
                visit_id=v.id,
                test_name="Hemoglobin A1c",
                value=8.9,
                unit="%",
                reference_low=4.0,
                reference_high=5.6,
                status=LabStatus.HIGH,
                loinc_code="4548-4",
            ),
            Allergy(
                patient_id=p.id,
                substance="Penicillin",
                reaction="rash",
                severity=AllergySeverity.MODERATE,
                noted_date=date(1985, 5, 5),
            ),
            FamilyHistory(
                patient_id=p.id,
                relationship_type="mother",
                condition="type 2 diabetes",
                icd10_code="E11.9",
                onset_age=52,
            ),
            MedicationStatement(
                patient_id=p.id,
                drug_name="Metformin",
                drug_class="Biguanide",
                dose="500 mg",
                frequency="BID",
                status=MedicationStatus.ACTIVE,
                start_date=date(2022, 3, 15),
            ),
            Procedure(
                patient_id=p.id,
                visit_id=v.id,
                description="Diabetic foot exam",
                performed_date=date(2026, 1, 10),
                provider_id=prov.id,
            ),
            Immunization(
                patient_id=p.id,
                vaccine="Influenza, seasonal",
                cvx_code="141",
                administered_date=date(2025, 10, 1),
                dose_number=1,
            ),
            VisitNote(visit_id=v.id, text="SOAP NOTE - test", author_id=prov.id),
        ]
    )
    s.flush()
    chronic = s.query(Condition).filter_by(icd10_code="E11.9").one()
    chronic.snomed_code = "44054006"
    chronic.snomed_display = "Type 2 diabetes mellitus"
    s.commit()
    s.refresh(p)
    yield p
    s.close()
    engine.dispose()


def _normalize(resources: list[dict], ported_only: bool) -> list[dict]:
    out = []
    for r in resources:
        r = json.loads(json.dumps(r))
        if ported_only and r["resourceType"] not in PORTED_TYPES:
            continue
        codings = r.get("code", {}).get("coding", [{}])
        if r["resourceType"] == "Observation" and codings and codings[0].get("code") == "55284-4":
            continue  # BP: deliberately reshaped (component-based) — asserted separately
        r.pop("id", None)
        r.pop("encounter", None)
        for field in DELIBERATE.get(r["resourceType"], ()):
            r.pop(field, None)
        out.append(r)
    out.sort(key=lambda r: (r["resourceType"], json.dumps(r, sort_keys=True)))
    return out


def test_golden_parity_for_ported_resources(golden_patient):
    """The refactor reproduces the old exporter's output, field for field."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden_norm = _normalize(golden, ported_only=True)
    new = build_bundle(golden_patient, strict=True)
    new_norm = _normalize([e["resource"] for e in new["entry"]], ported_only=True)
    assert new_norm == golden_norm


def test_deliberate_changes(golden_patient):
    """The three recorded deltas landed: gender fixed, real condition status."""
    bundle = build_bundle(golden_patient, strict=True)
    by_type: dict[str, list[dict]] = {}
    for entry in bundle["entry"]:
        by_type.setdefault(entry["resource"]["resourceType"], []).append(entry["resource"])
    assert by_type["Patient"][0]["gender"] == "female"  # the endswith("MALE") bug
    statuses = {
        c["code"]["coding"][0]["code"]: c["clinicalStatus"]["coding"][0]["code"] for c in by_type["Condition"]
    }
    assert statuses == {"E11.9": "active", "J06.9": "resolved"}
    bp = next(o for o in by_type["Observation"] if o["code"]["coding"][0]["code"] == "55284-4")
    components = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"] for c in bp["component"]}
    assert components == {"8480-6": 142, "8462-4": 88}  # decimals, not "142/88"
    assert "valueQuantity" not in bp


def test_new_chart_resources_present(golden_patient):
    """Every v0.4.0 entity exports (design §5 roster)."""
    bundle = build_bundle(golden_patient, strict=True)
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert {
        "AllergyIntolerance",
        "FamilyMemberHistory",
        "MedicationStatement",
        "Procedure",
        "Immunization",
        "DocumentReference",
        "Practitioner",
    } <= types
    doc = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "DocumentReference")
    import base64

    assert base64.b64decode(doc["content"][0]["attachment"]["data"]).decode() == "SOAP NOTE - test"


def test_snomed_enricher_is_additive(golden_patient):
    """The ontology enricher appends SNOMED without touching the ICD coding."""
    bundle = build_bundle(golden_patient, strict=True)
    e119 = next(
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Condition"
        and e["resource"]["code"]["coding"][0]["code"] == "E11.9"
    )
    systems = [c["system"] for c in e119["code"]["coding"]]
    assert systems == ["http://hl7.org/fhir/sid/icd-10", "http://snomed.info/sct"]
    assert e119["code"]["coding"][1]["code"] == "44054006"


def test_ids_are_stable_across_exports(golden_patient):
    """Review decision Q1: same chart, same ids — bundles diff cleanly."""
    ids_a = [e["resource"].get("id") for e in build_bundle(golden_patient)["entry"]]
    ids_b = [e["resource"].get("id") for e in build_bundle(golden_patient)["entry"]]
    assert ids_a == ids_b and all(ids_a)
    assert len(set(ids_a)) == len(ids_a)  # and unique within a bundle


def test_no_entity_leaks_into_output(golden_patient):
    """The transient _entity link never reaches the serialized bundle."""
    bundle = build_bundle(golden_patient, strict=True)
    assert "_entity" not in json.dumps(bundle)


def test_strict_discovery_loads_cleanly():
    """Fail-loud in tests (review decision Q3): every FHIR_MODULES entry
    must import and return enrichers."""
    enrichers = module_enrichers(strict=True)
    assert any(type(e).__name__ == "ConditionCodingEnricher" for e in enrichers)
    assert all(hasattr(e, "resource_type") and hasattr(e, "enrich") for e in enrichers)


def test_every_emitted_resource_is_conformant_r4b(golden_patient):
    """The fhir.resources validation gate (design §6, examined 2026-08-13):
    every resource build_bundle emits — including module enrichments —
    must validate against its official FHIR R4B model. Test-only
    dependency; any malformed emitter or enricher output fails CI here
    with pydantic's precise error."""
    fhir_r4b = pytest.importorskip("fhir.resources.R4B")

    bundle = build_bundle(golden_patient, strict=True)
    for entry in bundle["entry"]:
        resource = entry["resource"]
        model = fhir_r4b.get_fhir_model_class(resource["resourceType"])
        model.model_validate(resource)  # raises with field-level detail on any violation
    fhir_r4b.get_fhir_model_class("Bundle").model_validate(bundle)
