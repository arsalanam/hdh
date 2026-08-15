# Ontology guide (schema-registry demo + ICD→SNOMED tagging)

This module does two small things well — and demonstrates the **schema
registry** while doing them: it extends the core `Condition` entity with
`snomed_code`/`snomed_display` columns declaratively (no `models.py`
edits), and backfills those columns from a demo ICD-10→SNOMED map.

> Looking for the real SNOMED CT ontology — the full US Edition catalog,
> synonym search, subsumption, agent tools? That's the **snomed module**:
> [snomed.md](snomed.md). This module is the lightweight tagging bridge
> and the smallest possible schema-registry example.

## Usage

```bash
hdh schema           # what the registry loaded:
#   module load order: base → ontology_module → icd10cm_module → snomed_module
#   Condition [extends base]: snomed_code (ontology_module), snomed_display (ontology_module), concept_id (icd10cm_module)
#   OntologyConcept [NEW entity]: ... OntologyTerm ... OntologyClosure ...

hdh ontology tag     # backfill conditions.snomed_code/_display from the demo map
#   🏷  SNOMED-tagged 334 conditions (247 remain unmapped — extend ICD10_TO_SNOMED)
```

After tagging, FHIR `Condition` resources carry **both** codings (the
ontology module's enricher appends the SNOMED coding — see
`docs/design/fhir-emitters.md`):

```json
"coding": [
  {"system": "http://hl7.org/fhir/sid/icd-10", "code": "J06.9", "display": "Acute upper respiratory infection"},
  {"system": "http://snomed.info/sct",          "code": "54150009", "display": "Upper respiratory infection"}
]
```

Existing databases: `get_engine` auto-adds missing extension columns
(`ALTER TABLE ADD COLUMN`) — unless the database is under Alembic
management (an `alembic_version` table exists), in which case migrations
own the schema (see issue #30 for the one known gap).

## How the schema extension works

The module ships declarative specs (no Python for the schema part):

```
src/hdh/modules/ontology/
├── manifest.json                     {"name": "ontology_module", "depends_on": ["base"], ...}
└── schema/entities/condition.json    adds snomed_code, snomed_display to Condition
```

At startup, `bootstrap_schema()` (called by the CLI/tests/FHIR app) loads
every module listed in `hdh.modules.SCHEMA_MODULES`, merges specs under
the design's collision rules, and injects the columns into the mapped
classes — so `Condition.snomed_code` exists everywhere, queryable like
any other column. The icd10cm and snomed modules use the same mechanism
for the shared ontology tables. Full mechanics:
[ARCHITECTURE.md](../ARCHITECTURE.md) and `src/hdh/core/schema_registry.py`.

## Python API

```python
from hdh.core.models import Condition
from hdh.modules.ontology import ICD10_TO_SNOMED, snomed_for_icd10

snomed_for_icd10("I10")     # ("59621000", "Essential hypertension")

# after `hdh ontology tag`, it's just a column:
session.query(Condition).filter(Condition.snomed_code == "44054006")
```

## Where this is heading

- The hand-maintained `ICD10_TO_SNOMED` map is the known limitation:
  with both full catalogs now loaded in the shared tables (icd10cm +
  snomed modules), tagging should be **derived** — via the snomed
  `normalize()` funnel or curated `maps_to` edges. Tracked as
  [issue #29](https://github.com/arsalanam/hdh/issues/29) (relates to
  cross-ontology [#18](https://github.com/arsalanam/hdh/issues/18)).
- New generator conditions (the cardiometabolic pack) author their
  SNOMED codes directly on the `ConditionProfile` — profile-authored
  codes and this tagging path unify under #29.
- **Add your own schema module** — copy this module's shape (manifest +
  `schema/entities/*.json`), register it in `hdh.modules.SCHEMA_MODULES`,
  and your columns appear at next bootstrap. New entities, relationships,
  and even FHIR export hints are supported (see
  `tests/test_schema_registry.py` and `tests/test_fhir_declared.py`).

Verification tip: SNOMED concept IDs in the demo map are illustrative
for synthetic-data purposes; validate against an official SNOMED CT
release (which the snomed module can load with your own UMLS credential)
before using the mappings anywhere that matters.
