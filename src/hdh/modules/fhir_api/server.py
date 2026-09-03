"""
FastAPI app exposing the dataset as a (read-only) FHIR R4 facade.

Endpoints:
  GET /metadata                    — minimal CapabilityStatement
  GET /CarePlan?patient=<mrn>      — care plans, with their goals and activities
  GET /Patient?name=&_count=       — Patient search (searchset Bundle)
  GET /Patient/{mrn}               — Patient read
  GET /Patient/{mrn}/$everything   — full per-patient Bundle (core exporter)
"""

from hdh.core.exporters import patient_to_fhir_bundle
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
                        # Declared, because a capability statement that omits
                        # a served resource is worse than one that omits the
                        # endpoint: a client trusts it and never asks.
                        {
                            "type": "CarePlan",
                            "interaction": [{"code": "search-type"}],
                            "searchParam": [
                                {"name": "patient", "type": "reference"},
                                {"name": "status", "type": "token"},
                            ],
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

    @app.get("/CarePlan")
    def search_care_plans(patient: str = "", status: str = "", _count: int = 20):
        """Care plans for one patient, as a searchset Bundle.

        `patient` takes an MRN, which is what a CarePlan's subject resolves
        to in this export — the chart's own anchor, not a surrogate id.

        Filtered from the patient's full bundle rather than built here, so
        the resource a receiver gets from `/CarePlan` is byte-identical to
        the one in `$everything`. Two code paths producing two shapes of the
        same resource is how an export starts disagreeing with itself.
        """
        session = db()
        try:
            if not patient:
                raise HTTPException(
                    status_code=400,
                    detail="CarePlan search requires ?patient=<mrn>",
                )
            p = session.query(Patient).filter(Patient.mrn == patient).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Patient {patient} not found")
            bundle = patient_to_fhir_bundle(p)
            plans = [
                entry
                for entry in bundle.get("entry") or []
                if (entry.get("resource") or {}).get("resourceType") == "CarePlan"
            ]
            if status:
                wanted = {s.strip() for s in status.split(",") if s.strip()}
                plans = [e for e in plans if (e["resource"].get("status") or "") in wanted]
            return {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(plans),
                "entry": plans,
            }
        finally:
            session.close()

    @app.get("/Patient")
    def search_patients(name: str = "", _count: int = 20):
        session = db()
        try:
            q = session.query(Patient)
            if name:
                like = f"%{name}%"
                q = q.filter(Patient.first_name.ilike(like) | Patient.last_name.ilike(like))
            patients = q.limit(min(_count, 100)).all()
            return {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(patients),
                "entry": [{"resource": _fhir_patient(p)} for p in patients],
            }
        finally:
            session.close()

    return app
