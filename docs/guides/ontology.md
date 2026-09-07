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

hdh ontology tag     # backfill conditions.snomed_code/_display (three-source derivation)
#   🏷  SNOMED-tagged 512 conditions (39 profile-authored, 152 curated, 321 derived
#      from the loaded catalogs) · 47 maps_to edges recorded · 69 remain unmapped
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

## How tagging derives its mappings (issue #29 — built)

`hdh ontology tag` assembles its mapping table from three sources, in
precedence order:

1. **Profile-authored** (confidence 1.0) — the generator's own
   `ConditionProfile`s carry SNOMED codes (including staged codes like
   CKD 3a→5); for generated data the pack author's code is the answer.
2. **Curated** (confidence 1.0) — the `ICD10_TO_SNOMED` demo map, kept
   as explicit curation for codes profiles don't cover.
3. **Derived** (confidence = funnel score ≥ 0.6) — the SNOMED module's
   `normalize()` funnel over the loaded US Edition, constrained to
   disorder/finding semantic tags. Needs the catalog loaded
   ([snomed guide](snomed.md)); silently contributes nothing otherwise.

Mappings also materialize as **`maps_to` edges** (authority
`PACK_AUTHORED` / `CURATED_DEMO` / `DERIVED_NORMALIZE`, confidence
carried) when both concepts exist in the shared tables. These are the
*derived* authorities; the *official* NLM and LOINC maps load separately
through `hdh ontology crosswalk` (below), under their own authorities, and
each loader touches only its own — the derived edges and the official ones
coexist.

Ask for one from either side:

```bash
hdh icd lookup E11.9
#   E11.9 — Type 2 diabetes mellitus without complications  [billable]
#      └ E00-E89: Endocrine, nutritional and metabolic diseases
#        └ E08-E13: Diabetes mellitus (E08-E13)
#          └ E11: Type 2 diabetes mellitus
#      maps to → snomed_ct:44054006 Type 2 diabetes mellitus  [PACK_AUTHORED]
```

The authority is printed because an asserted mapping and one a funnel
derived at 0.87 are different things to trust; a derived edge shows its
score.

## Official crosswalks — the licensed maps (issue #86)

The three authorities above are *ours*: codes the pack author wrote, a
curated starter map, and funnel-derived guesses. The **official** maps —
the NLM ICD-10-CM ↔ SNOMED CT crosswalk, and LOINC's equivalents — are
licensed files a user downloads under their own UMLS/LOINC credential and
nobody may redistribute. So hdh ships the loader, never the data (the same
rule as SNOMED, LOINC and RxNorm), through the same `LoadStage` pipeline:

```bash
# a file YOU obtained under your own licence — hdh never ships it
hdh ontology crosswalk --source ICD10CM_SNOMED_MAP_US_20260301.txt
#   acquire      nlm-icd10cm-snomed, release 202603, sha ae3e27807bdbba95
#   parse        93,114 pairs parsed (0 blank rows skipped)
#   match        71,203 edges matched; skipped 0 with no icd10cm concept,
#                21,911 with no snomed_ct concept
#   load-edges   71,203 'NLM_UMLS' maps_to edges written (0 replaced)
#   verify       71,203 edges, no dangling targets
#   finalize     icd10cm_snomed release 202603 recorded (4.1s)

hdh ontology crosswalk-status          # what is loaded, and live edge counts
```

`--map` is inferred from the file's header (`nlm-icd10cm-snomed` or
`loinc-snomed`); pass it explicitly to be sure. A new licensed map is a
new `MapSpec` entry, not a new loader.

Two properties are deliberate. An edge is written **only where both
concepts are already loaded** — the map is authoritative about the
relationship, not about what is in your database, and a pair whose SNOMED
concept you never loaded is reported and skipped rather than smuggled in as
a stub. And the load **rebuilds only its own authority** (`NLM_UMLS`,
`LOINC_SNOMED`): your derived `PACK_AUTHORED`/`DERIVED_NORMALIZE` edges are
left exactly as they were, so `hdh icd lookup` shows both the official
mapping and the derived one side by side, each with its authority.

These edges target `snomed_ct:` concepts, so `just release-check` already
refuses any asset containing them and `hdh snomed purge` removes them —
nothing licensed can leak through the crosswalk that could not leak through
SNOMED itself.

> **Cannot be verified end to end here.** The NLM and LOINC maps do not
> ship, so the loader is tested against a synthetic fixture in each file's
> format and no further. Expect the first run against a real UMLS release
> to surface something the fixture did not — this repo's real defects tend
> to show up only on real data.

## Mapped, but not a problem

Every ICD-10 code the generator emits maps to a SNOMED concept — and
**seven of them are not problems.** An annual physical is
`162673000 General examination of patient`, a well-child visit is
`410620009 Well child visit`, and a fall is `217082002 Accidental fall`:
five procedures and an event. Correct mappings, and none of them belongs
on a problem list.

So a profile records the hierarchy alongside the code:

```python
_SNOMED = {
    "type2_diabetes":         ("44054006",  "disorder"),
    "stroke_history":         ("275526006", "situation"),   # "History of..."
    "annual_physical_adult":  ("162673000", "procedure"),   # mapped, not a problem
    "fall_injury":            ("217082002", "event"),
}
```

FHIR `Condition.code` is bound to problems, diagnoses and health
concerns, so `ConditionCodingEnricher` appends a coding only for
`disorder`, `finding` and `situation` — a *situation with explicit
context* qualifies, since "history of stroke" really is a problem-list
entry. The mapping still exists and `hdh icd lookup` still shows it; it
simply never claims to be the patient's problem.

The hierarchy is recorded rather than looked up because the consumer
cannot look it up: a FHIR enricher gets the entity and no database
session, by design.

## Licensing — what ships, and what you supply

| Ontology | License reality | What hdh does |
|---|---|---|
| **ICD-10-CM** | public domain | full catalog ships and downloads (`hdh icd load --download`) |
| **SNOMED CT** | UMLS license — free for US affiliates, **not redistributable** | loader ships, data never; `hdh snomed load --download` with your own UMLS key. The starter map ships as `maps_to` edges, not as SNOMED content; the official NLM ICD-10-CM↔SNOMED crosswalk loads via `hdh ontology crosswalk` |
| **LOINC** | free with registration, redistribution restricted | loader ships; you supply the release (`hdh loinc load --source <dir>`) and the LOINC↔SNOMED map (`hdh ontology crosswalk --map loinc-snomed`) |
| **RxNorm** | UMLS license | loader ships; you supply the release (`hdh rxnorm load --source <dir>`) |
| **CPT** | AMA-copyrighted, **paid** | the schema supports it; hdh will never ship it |
| **ICD-10-PCS / HCPCS** | public domain | future loaders, same `LoadStage` pipeline |

The rule is one line: **loaders ship, licensed data never does.** Release
builds are gated by `just release-check`, which fails on any licensed row
in a release asset — see CONTRIBUTING.
- **Add your own schema module** — copy this module's shape (manifest +
  `schema/entities/*.json`), register it in `hdh.modules.SCHEMA_MODULES`,
  and your columns appear at next bootstrap. New entities, relationships,
  and even FHIR export hints are supported (see
  `tests/test_schema_registry.py` and `tests/test_fhir_declared.py`).

Verification tip: SNOMED concept IDs in the demo map are illustrative
for synthetic-data purposes; validate against an official SNOMED CT
release (which the snomed module can load with your own UMLS credential)
before using the mappings anywhere that matters.
