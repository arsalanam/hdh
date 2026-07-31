# FHIR API guide

Serves the dataset as a read-only FHIR R4 REST API.

```bash
pip install -e ".[api]"       # fastapi, uvicorn
hdh serve --port 8000
# 🌐 FHIR R4 API → http://127.0.0.1:8000  (docs at /docs)
```

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /metadata` | Minimal `CapabilityStatement` |
| `GET /Patient/{mrn}` | The `Patient` resource |
| `GET /Patient?name=smith&_count=20` | `searchset` Bundle of matching Patients (name substring; `_count` capped at 100) |
| `GET /Patient/{mrn}/$everything` | The full per-patient Bundle: Patient, one Encounter per visit, Observations for vitals and labs (LOINC-coded, with reference ranges), Conditions (ICD-10), MedicationRequests |
| `GET /docs` | Interactive OpenAPI docs (FastAPI) |

```bash
curl -s localhost:8000/Patient/MRN12345678 | jq .name
curl -s "localhost:8000/Patient?name=ward" | jq .total
curl -s "localhost:8000/Patient/MRN12345678/\$everything" | jq '.entry | length'
```

Unknown MRNs return `404` with a JSON detail body.

## Design

The API is a thin facade over `hdh.core.exporters.patient_to_fhir_bundle` —
the same serialization used by `hdh export --format fhir`, so file exports and
API responses can never drift apart. Each request opens and closes its own
SQLAlchemy session against the SQLite file.

Embed it in your own service:

```python
from hdh.modules.fhir_api.server import create_app
app = create_app(db_path="family_medicine.db")   # a FastAPI app — mount or run it
```

## Limitations & extension path

This is a testing facade, not a conformant FHIR server:

- Read-only; no create/update, no transactions, no auth.
- Search supports `name` only; no `_include`, pagination tokens, or `_sort`.
- Resources carry no persistent server-assigned IDs beyond the MRN.

Natural next steps: token auth middleware, search on birthdate/identifier,
per-resource endpoints (`/Observation?patient=`), and SNOMED codings from the
ontology module in `Condition.code.coding`.
