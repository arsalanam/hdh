"""
FastAPI app exposing the dataset as a (read-only) FHIR R4 facade.

Endpoints:
  GET /metadata                                        — minimal CapabilityStatement
  GET /Patient?name=&birthdate=&identifier=&_count=     — Patient search (searchset Bundle)
  GET /Patient/{mrn}                                    — Patient read
  GET /Patient/{mrn}/$everything                        — full per-patient Bundle (core exporter)
  GET /Observation?patient=&category=&_count=           — Observation search (vitals and/or labs)
"""

from datetime import date

from hdh.core.exporters import (
    _fhir_lab_observations,
    _fhir_vitals_observations,
    patient_to_fhir_bundle,
)
from hdh.core.models import Patient, get_engine, get_session


def _fhir_patient(p: Patient) -> dict:
    bundle = patient_to_fhir_bundle(p)
    return bundle["entry"][0]["resource"]


def create_app(db_path: str = "family_medicine.db"):
    """Build the FastAPI app serving the read-only FHIR R4 facade."""
    from fastapi import FastAPI, HTTPException

    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    app = FastAPI(
        title="hdh FHIR R4 API",
        description="Read-only FHIR R4 facade over the synthetic family-medicine dataset",
    )
    engine = get_engine(db_path)

    def db():
        return get_session(engine)

    @app.get("/metadata")
    def metadata():
        return {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "kind": "instance",
            "fhirVersion": "4.0.1",
            "format": ["json"],
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": "Patient",
                            "interaction": [{"code": "read"}, {"code": "search-type"}],
                            "operation": [{"name": "everything", "definition": "Patient-everything"}],
                        },
                        {
                            "type": "Observation",
                            "interaction": [{"code": "search-type"}],
                        },
                    ],
                }
            ],
        }

    @app.get("/Patient/{mrn}")
    def read_patient(mrn: str):
        session = db()
        try:
            p = session.query(Patient).filter(Patient.mrn == mrn).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Patient {mrn} not found")
            return _fhir_patient(p)
        finally:
            session.close()

    @app.get("/Patient/{mrn}/$everything")
    def patient_everything(mrn: str):
        session = db()
        try:
            p = session.query(Patient).filter(Patient.mrn == mrn).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Patient {mrn} not found")
            return patient_to_fhir_bundle(p)
        finally:
            session.close()

    @app.get("/Patient")
    def search_patients(name: str = "", birthdate: str = "", identifier: str = "", _count: int = 20):
        session = db()
        try:
            q = session.query(Patient)
            if name:
                like = f"%{name}%"
                q = q.filter(Patient.first_name.ilike(like) | Patient.last_name.ilike(like))
            if birthdate:
                try:
                    dob = date.fromisoformat(birthdate)
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid birthdate '{birthdate}', expected YYYY-MM-DD",
                    )
                q = q.filter(Patient.date_of_birth == dob)
            if identifier:
                q = q.filter(Patient.mrn == identifier)
            patients = q.limit(min(_count, 100)).all()
            return {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(patients),
                "entry": [{"resource": _fhir_patient(p)} for p in patients],
            }
        finally:
            session.close()

    @app.get("/Observation")
    def search_observations(patient: str = "", category: str = "", _count: int = 20):
        if not patient:
            raise HTTPException(status_code=400, detail="the 'patient' search parameter is required")
        if category not in ("", "vital-signs", "laboratory"):
            raise HTTPException(
                status_code=400,
                detail="category must be 'vital-signs' or 'laboratory' when supplied",
            )
        session = db()
        try:
            p = session.query(Patient).filter(Patient.mrn == patient).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Patient {patient} not found")

            observations: list[dict] = []
            for v in p.visits:
                enc_id = str(v.id)
                if category in ("", "vital-signs"):
                    observations.extend(_fhir_vitals_observations(v, p.mrn, enc_id))
                if category in ("", "laboratory"):
                    observations.extend(_fhir_lab_observations(v, p.mrn, enc_id))

            observations = observations[: min(_count, 100)]
            return {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(observations),
                "entry": [{"resource": o} for o in observations],
            }
        finally:
            session.close()

    return app
