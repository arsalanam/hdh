# FHIR Export as Pluggable Emitters — Design (Draft)

**Target:** `hdh.core.exporters` + a new module extension point · **Status:**
draft for review · **Date:** 2026-08-13

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Problem identification (hardcoded schema review), extensibility requirement |
| | | |

---

## Contents

1. [Motivation](#1-motivation)
2. [Options considered](#2-options-considered)
3. [The design: emitters, enrichers, discovery](#3-the-design-emitters-enrichers-discovery)
4. [Terminology constants as data](#4-terminology-constants-as-data)
5. [What core ships; what modules own](#5-what-core-ships-what-modules-own)
6. [Typed resources (fhir.resources) — follow-up, not blocker](#6-typed-resources-fhirresources--follow-up-not-blocker)
7. [Phase 2: schema-registry `fhir` hints](#7-phase-2-schema-registry-fhir-hints)
8. [Testing and migration](#8-testing-and-migration)
9. [Open questions](#9-open-questions)

---

## 1. Motivation

`core/exporters.py` hand-builds every FHIR resource in one 500-line file.
Three concrete problems, in increasing order of severity:

1. **Hardcoded terminology.** System URIs, the visit-type → encounter-class
   map, and category codings are string literals scattered through
   functions — the honestly config-shaped fragments are trapped in code.
2. **The keyhole hack.** `_dx_codings()` does
   `getattr(dx, "snomed_code", None)` — core *groping* for a column it
   hopes the ontology module added. It works, but the pattern scales as
   one `getattr` per future coding source (RxNorm on medications, LOINC
   refinements, icd10cm concept references), each one core silently
   depending on a module's internals.
3. **The roadmap cannot land here.** The comprehension service must emit
   `Composition`/`DocumentReference`; the care-plan module must emit
   `CarePlan`/`Goal`/`ServiceRequest`/`CareTeam`. Under the current
   design, both would edit core. Storage solved this exact problem with
   the schema registry; **output needs the same move.**

Also due: the v0.4.0 chart entities (Allergy, FamilyHistory,
MedicationStatement, Procedure, Immunization, VisitNote) have no FHIR
representation yet — this refactor is the natural vehicle.

## 2. Options considered

| Option | Verdict |
|---|---|
| **Mapping DSL** (YAML/JSON field maps → FHIR paths) | **Rejected.** FHIR construction is conditional, nested, and reference-laced; a DSL expressive enough becomes a worse programming language in YAML — untyped, untestable at the mapping level, undebuggable, and less teachable than the Python it replaces. Config earns its keep when non-programmers edit mappings or mappings vary per deployment; neither holds for hdh's single canonical shape. |
| **HL7 StructureMap / FHIR Mapping Language** | Named for completeness; the standards-true answer. Python engine support is immature and the ceremony is disproportionate to hdh's scale. Revisit only if hdh ever needs *user-supplied* mappings. |
| **Pluggable emitters + enrichers** (the `GapFinder`/`LoadStage`/`OntologyService` move applied to output) | **Chosen** — §3. Logic stays in typed, testable Python; the genuinely data-shaped fragments become data (§4); modules extend output the way the registry lets them extend storage. |

## 3. The design: emitters, enrichers, discovery

Two small protocols in `hdh.core.fhir` (new module; `exporters.py` keeps
JSON/text and the bundle assembly):

```python
@dataclass(frozen=True)
class ExportContext:
    """Per-export state emitters share: the patient, MRN, id allocator."""
    patient: Patient
    mrn: str
    next_id: Callable[[str], str]        # stable per-resource-type ids


class FhirEmitter(Protocol):
    """Builds resources of one type from chart entities."""
    resource_type: ClassVar[str]         # "Condition", "Observation", …
    def emit(self, ctx: ExportContext) -> list[dict]: ...


class FhirEnricher(Protocol):
    """Decorates already-built resources — the getattr hack's replacement."""
    resource_type: ClassVar[str]         # which resources it touches
    def enrich(self, resource: dict, entity, ctx: ExportContext) -> None: ...
```

**Assembly** (`patient_to_fhir_bundle`, unchanged public signature):
run every registered emitter, then every enricher whose `resource_type`
matches, then wrap in the Bundle. Emitters attach the source entity to
each resource transiently so enrichers receive `(resource, entity, ctx)`
and never re-query.

**Discovery** mirrors `CLI_MODULES` / `SCHEMA_MODULES`:

```python
# hdh/modules/__init__.py
FHIR_MODULES = (
    "hdh.modules.ontology.fhir",   # ConditionCodingEnricher (SNOMED)
)
```

Core's own emitters register unconditionally; module contributions load
lazily and fail soft (an absent optional module never breaks export —
same contract as agent tools).

**Ordering rules** (the part that prevents subtle bugs): emitters are
independent and unordered; enrichers run after all emitters, in module
discovery order; an enricher may append codings/extensions but must not
delete or replace what an emitter built — additive only, stated in the
protocol docstring and checked in tests.

## 4. Terminology constants as data

The config-shaped fragments extract to `hdh.core.fhir.terminology` — plain
module-level data, no file format ceremony:

```python
SYSTEMS = {
    "icd10": "http://hl7.org/fhir/sid/icd-10",
    "snomed": "http://snomed.info/sct",
    "loinc": "http://loinc.org",
    "cvx": "http://hl7.org/fhir/sid/cvx",
    ...
}
ENCOUNTER_CLASS = {"acute": "AMB", "follow_up": "AMB", "preventive": "AMB", "urgent": "EMER"}
```

Lookups as data, logic as code — the line this design holds.

## 5. What core ships; what modules own

**Core emitters** (one small class each — also dissolves the 500-line
file the quality gate keeps flagging):

| Emitter | Source | Status |
|---|---|---|
| Patient, Encounter, Condition, Observation (vitals), Observation (labs), MedicationRequest | today's functions, split | port |
| **AllergyIntolerance** | `Allergy` | new (v0.4.0 gap) |
| **FamilyMemberHistory** | `FamilyHistory` (+ lightweight `FamilyMember` summaries as notes) | new |
| **MedicationStatement** | `MedicationStatement` | new |
| **Procedure** | `Procedure` | new |
| **Immunization** | `Immunization` (CVX-coded) | new |
| **DocumentReference** | `VisitNote` (the stored SOAP text, base64) | new |
| **Practitioner** | `Provider` (thin, referenced from Encounter) | new |

**Module contributions:**

- `ontology` module: `ConditionCodingEnricher` — appends the SNOMED
  coding from *its own* columns; `_dx_codings`'s `getattr` is deleted.
- `icd10cm` module (follow-up): enricher adding graph-concept references.
- Future `comprehension` / `careplan`: full **emitters** (Composition,
  CarePlan, Goal…) registered from their own packages — zero core edits.

The FHIR API module (`hdh serve`) needs no change: it serves
`patient_to_fhir_bundle`, which now simply returns more.

## 6. Typed resources (fhir.resources) — ADOPTED (2026-08-13)

The `fhir.resources` pydantic package makes every emitter construct
validated, typed R4B resources instead of bare dicts — fixing the quality
gate's standing `bare dict` warnings at the root. Deliberately kept out
of the refactor PR (it multiplies the diff without changing structure),
then landed in two steps:

1. **Conformance gate first** (PR #24): a test validates every emitted
   resource and the whole Bundle against the official R4B models. This
   alone surfaced two real defects — the `"142/88"` BP string in a FHIR
   decimal (now a component-based Observation, LOINC 8480-6/8462-4) and
   a bare date in `DocumentReference.date` (a FHIR *instant*).
2. **Full typed construction**: emitters build `fhir.resources.R4B`
   models directly and return `(model, entity)` pairs; enrichers mutate
   typed models (e.g. appending a typed `Coding`); the bundle assembler
   wraps them in a typed `Bundle` and serializes **once** with
   `model_dump(mode="json", exclude_none=True)`. `fhir.resources` moved
   from the dev group to a core dependency.

Rationale for adopting rather than deferring indefinitely: malformed
resources now fail *at the line that builds them* with a pydantic error
naming the field, and every legal field autocompletes — the strongest
guardrail available for both human and AI authors of future emitters
(comprehension, care-plan). Measured costs are negligible for a batch
exporter: ~0.2 ms validation per resource, ~1.8 s one-time import.

## 7. Phase 2: schema-registry `fhir` hints

Future synergy, recorded not built: an entity JSON may carry an optional
hint block —

```json
"fhir": {"resourceType": "Observation", "fields": {"value": "valueQuantity.value"}}
```

— and a generic `DeclaredEntityEmitter` exports *flat* declared entities
for free, with hand-written emitters reserved for resources with real
logic. This is the mapping-DSL idea returning at the right altitude:
per-entity hints for trivial shapes, never a whole-bundle language.
Build it when a schema module actually ships a flat entity that needs
export, not before.

## 8. Testing and migration

- **Golden-bundle test**: capture today's bundle for a fixture patient;
  after the refactor the ported emitters must reproduce it byte-for-byte
  (minus deliberate additions) — the refactor's no-regression proof.
- Per-emitter unit tests (one entity in → resource shape out); enricher
  additivity test (enricher runs → original keys untouched).
- FHIR API round-trip already in the suite extends to the new resources.
- Migration is code-only: no schema change, no data change, public
  functions keep their signatures. One PR, reviewable in one sitting.

## 9. Open questions

1. **Resource ids**: today MRN-anchored and positional. Keep positional
   (`Observation/{mrn}-lab-3`) or derive stable hashes so re-exports of
   an unchanged chart are id-stable? (Matters for consumers that diff
   bundles.)  ...my gut instinct says stable hashes will serve better for evalauting for example care plans

2. **DocumentReference vs Composition** for stored notes now — DR is the
   simple truthful choice; Composition belongs to the comprehension
   service later. Confirm DR. DR confirmed
3. **Enricher failure policy**: fail-soft (log, skip) like tool loading,
   or fail-loud like load stages? Lean fail-loud in tests, fail-soft at
   runtime.
Yes failure loud at testing is good enough 
4. 
4. **Should JSON/text exporters adopt the same emitter pattern?** Lean
   no — they are flat and stable; FHIR is where extension pressure lives.
No is best answer