"""Tests for the FHIR R4 API facade (src/hdh/modules/fhir_api/server.py)."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hdh.core.generators import build_dataset
from hdh.core.models import Patient, get_engine, get_session
from hdh.modules.fhir_api.server import create_app


@pytest.fixture(scope="module")
def api_db_path():
    """A small file-based SQLite dataset the FastAPI app can open independently
    (a fresh engine per request needs a real file, not a :memory: DB)."""
    tmp_dir = tempfile.mkdtemp()
    db_path = str(Path(tmp_dir) / "fhir_api_test.db")

    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(db_path)
    session = get_session(engine)
    build_dataset(session, n_patients=12, years_of_history=2, verbose=False)
    session.close()
    return db_path


@pytest.fixture(scope="module")
def client(api_db_path):
    app = create_app(db_path=api_db_path)
    return TestClient(app)


@pytest.fixture(scope="module")
def sample_patient(api_db_path):
    """A real patient from the seeded dataset, used to test exact-match filters."""
    engine = get_engine(api_db_path)
    session = get_session(engine)
    try:
        return session.query(Patient).first()
    finally:
        session.close()


class TestPatientSearchBirthdate:
    def test_matches_exact_birthdate(self, client, sample_patient):
        resp = client.get("/Patient", params={"birthdate": sample_patient.date_of_birth.isoformat()})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        mrns = [e["resource"]["id"] for e in body["entry"]]
        assert sample_patient.mrn in mrns

    def test_no_match_for_unused_birthdate(self, client):
        resp = client.get("/Patient", params={"birthdate": "1901-01-01"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_invalid_birthdate_is_400(self, client):
        resp = client.get("/Patient", params={"birthdate": "not-a-date"})
        assert resp.status_code == 400


class TestPatientSearchIdentifier:
    def test_matches_exact_mrn(self, client, sample_patient):
        resp = client.get("/Patient", params={"identifier": sample_patient.mrn})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["entry"][0]["resource"]["id"] == sample_patient.mrn

    def test_unknown_identifier_returns_empty_bundle(self, client):
        resp = client.get("/Patient", params={"identifier": "MRN00000000"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestObservationSearch:
    def test_requires_patient_param(self, client):
        resp = client.get("/Observation")
        assert resp.status_code == 400

    def test_unknown_patient_is_404(self, client):
        resp = client.get("/Observation", params={"patient": "MRN00000000"})
        assert resp.status_code == 404

    def test_returns_observations_for_known_patient(self, client, sample_patient):
        resp = client.get("/Observation", params={"patient": sample_patient.mrn})
        assert resp.status_code == 200
        body = resp.json()
        assert body["resourceType"] == "Bundle"
        assert body["type"] == "searchset"
        assert all(e["resource"]["resourceType"] == "Observation" for e in body["entry"])

    def test_category_laboratory_only_returns_lab_observations(self, client, sample_patient):
        resp = client.get("/Observation", params={"patient": sample_patient.mrn, "category": "laboratory"})
        assert resp.status_code == 200
        body = resp.json()
        for entry in body["entry"]:
            categories = entry["resource"].get("category", [])
            assert categories, "lab Observations must carry a category block"
            codes = [c["code"] for coding in categories for c in coding["coding"]]
            assert "laboratory" in codes

    def test_category_vital_signs_and_laboratory_are_disjoint(self, client, sample_patient):
        resp_labs = client.get("/Observation", params={"patient": sample_patient.mrn, "category": "laboratory"})
        resp_vitals = client.get("/Observation", params={"patient": sample_patient.mrn, "category": "vital-signs"})
        assert resp_vitals.status_code == 200
        vitals_ids = {e["resource"]["id"] for e in resp_vitals.json()["entry"]}
        lab_ids = {e["resource"]["id"] for e in resp_labs.json()["entry"]}
        assert vitals_ids.isdisjoint(lab_ids)

    def test_invalid_category_is_400(self, client, sample_patient):
        resp = client.get("/Observation", params={"patient": sample_patient.mrn, "category": "bogus"})
        assert resp.status_code == 400

    def test_count_caps_results(self, client, sample_patient):
        resp = client.get("/Observation", params={"patient": sample_patient.mrn, "_count": 1})
        assert resp.status_code == 200
        assert len(resp.json()["entry"]) <= 1
