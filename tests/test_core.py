from hdh.core.disease_engine import CONDITIONS, pick_condition
from hdh.core.exporters import patient_to_fhir_bundle, patient_to_json, patient_to_text
from hdh.core.models import Diagnosis, Patient, Visit


def test_dataset_generated(db_session):
    assert db_session.query(Patient).count() == 8
    assert db_session.query(Visit).count() > 0
    assert db_session.query(Diagnosis).count() > 0


def test_visits_have_clinical_detail(db_session):
    visit = db_session.query(Visit).first()
    assert visit.vitals is not None
    assert visit.diagnoses
    assert visit.chief_complaint


def test_disease_engine():
    assert len(CONDITIONS) >= 30
    profile, name = pick_condition(age=45, month=1, existing_conditions=set())
    assert name in CONDITIONS
    assert profile.icd10_code


def test_json_export(db_session):
    p = db_session.query(Patient).first()
    data = patient_to_json(p)
    assert data["mrn"] == p.mrn
    assert data["total_visits"] == len(data["visits"])


def test_fhir_export(db_session):
    p = db_session.query(Patient).first()
    bundle = patient_to_fhir_bundle(p)
    assert bundle["resourceType"] == "Bundle"
    resource_types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert "Patient" in resource_types
    assert "Encounter" in resource_types


def test_text_export(db_session):
    p = db_session.query(Patient).first()
    text = patient_to_text(p)
    assert p.mrn in text
    assert "PATIENT CHART SUMMARY" in text
