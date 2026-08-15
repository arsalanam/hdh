from hdh.core.conditions import SamplingContext, default_catalog
from hdh.core.exporters import patient_to_fhir_bundle, patient_to_json, patient_to_text
from hdh.core.models import Condition, Patient, Sex, Visit


def test_dataset_generated(db_session):
    assert db_session.query(Patient).count() == 8
    assert db_session.query(Visit).count() > 0
    assert db_session.query(Condition).count() > 0


def test_visits_have_clinical_detail(db_session):
    visit = db_session.query(Visit).first()
    assert visit.vitals is not None
    assert visit.conditions
    assert visit.chief_complaint


def test_disease_engine():
    catalog = default_catalog()
    assert len(catalog.names()) >= 30
    profile = catalog.sample_visit_condition(SamplingContext(age=45, sex=Sex.FEMALE, month=1))
    assert profile.name in catalog.names()
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
