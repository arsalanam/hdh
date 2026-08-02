# Ontology guide

Maps ICD-10 diagnosis codes to SNOMED CT — and demonstrates the **schema
registry**: this module extends the core `Diagnosis` entity with two new
columns declaratively, without touching `models.py`.

## Usage

```bash
hdh schema           # show what the registry loaded:
#   module load order: base → ontology_module
#   Diagnosis [extends base]: snomed_code (ontology_module), snomed_display (ontology_module)

hdh ontology tag     # backfill diagnoses.snomed_code/_display from the ICD-10 map
#   🏷  SNOMED-tagged 106,767 diagnoses (59,205 remain unmapped — extend ICD10_TO_SNOMED)
```

After tagging, FHIR `Condition` resources carry **both** codings:

```json
"coding": [
  {"system": "http://hl7.org/fhir/sid/icd-10", "code": "J06.9", "display": "Acute upper respiratory infection"},
  {"system": "http://snomed.info/sct",          "code": "54150009", "display": "Upper respiratory infection"}
]
```

Works on existing databases too: `get_engine` auto-adds the extension columns
(`ALTER TABLE ADD COLUMN`) the first time it opens a file generated before
the module existed.

## How the schema extension works

The module ships declarative specs (no Python for the schema part):

```
src/hdh/modules/ontology/
├── manifest.json                      {"name": "ontology_module", "depends_on": ["base"], ...}
└── schema/entities/diagnosis.json    adds snomed_code, snomed_display to Diagnosis
```

At startup, `bootstrap_schema()` (called by the CLI/tests/FHIR app) loads
every module listed in `hdh.modules.SCHEMA_MODULES`, merges specs under the
design's collision rules, and injects the columns into the mapped classes —
so `Diagnosis.snomed_code` exists everywhere, queryable like any other
column. Full mechanics: [ARCHITECTURE.md §3](../ARCHITECTURE.md) and
`src/hdh/core/schema_registry.py`.

## Python API

```python
from hdh.modules.ontology import snomed_for_icd10, ICD10_TO_SNOMED

snomed_for_icd10("I10")     # ("59621000", "Essential hypertension")

# after `hdh ontology tag`, it's just a column:
session.query(Diagnosis).filter(Diagnosis.snomed_code == "44054006")
```

## Extending

1. **Complete the map** — add the remaining disease-engine codes
   (`hdh list-conditions`) to `ICD10_TO_SNOMED`, re-run `hdh ontology tag`.
2. **Add your own schema module** — copy the ontology module's shape
   (manifest + `schema/entities/*.json`), register it in
   `hdh.modules.SCHEMA_MODULES`, and your columns appear at next bootstrap.
   New entities and relationships are supported too (see
   `tests/test_schema_registry.py` for spec examples).
3. **Terminology server** — replace the static dict with `$translate`
   lookups against a FHIR terminology service, keeping the dict as cache.

Verification tip: SNOMED concept IDs here are illustrative for synthetic-data
purposes; validate against an official SNOMED CT release before using the
mappings anywhere that matters.
