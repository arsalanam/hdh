# Core Patient Chart Expansion — Design (Draft)

**Target:** `hdh.core` (models + generators + exporters) · **Status:** draft
for review · **Date:** 2026-08-11

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Gap analysis, core-vs-module decision, thin-entity principle |
| | | |

---

## Contents

1. [Motivation: the gap analysis](#1-motivation-the-gap-analysis)
2. [The decision: core, not a module](#2-the-decision-core-not-a-module)
3. [The thin-entity principle](#3-the-thin-entity-principle)
4. [New and changed entities](#4-new-and-changed-entities)
5. [Concept links](#5-concept-links)
6. [Generation: making the new chart real](#6-generation-making-the-new-chart-real)
7. [Migration and back-compatibility](#7-migration-and-back-compatibility)
8. [Ripple effects across modules](#8-ripple-effects-across-modules)
9. [Testing](#9-testing)
10. [Phases](#10-phases)
11. [Open questions](#11-open-questions)

---

## 1. Motivation: the gap analysis

The core model was built as a *generation* substrate: six tables that let a
disease engine emit realistic visits. Measured against what a minimal EHR
chart maintains (reference points: GNU Health's patient core; FHIR's chart
resources), the gaps:

| Chart requirement | Today | Gap |
|---|---|---|
| Patient profile | demographics, blood type, flat insurance strings | deceased flag/date, marital status, language, emergency contact |
| Family structure | — | no family/relationship modeling at all |
| Family history | 4 booleans | cannot express "mother, breast cancer, onset 52" |
| Past conditions | `ChronicCondition` (ongoing, `controlled` flag) | no status lifecycle (active/resolved/remission), no resolution date |
| Treatments — past & active | per-visit `Prescription` orders | no cross-visit medication list with status and indication; no procedures; no immunizations |
| Notes at visit | narrative renders on demand; nothing stored | no stored clinical note — the comprehension service's planned input |
| Encounters | `Visit` with provider as free text | no `Provider` identity |
| Allergies | pipe-separated string | no substance/reaction/severity structure |

Explicit non-goals (GNU Health has them; a *basic* chart does not):
scheduling/appointments, hospital infrastructure (wards/beds/ORs),
birth/death certificates, billing lifecycle, inventory.

## 2. The decision: core, not a module

The basic chart lands in **`hdh.core.models`**, not a schema-registry
module. Rationale (the decision this document exists to record):

- **The chart is the domain.** Family history, medication lists, notes,
  and allergies are not features layered on an EHR — they are what an EHR
  *is*. Every planned module (comprehension, care-plan, SNOMED tools)
  consumes them; a foundation should not live in an optional add-on.
- **Generation must change anyway.** The one-time cost of updating the
  generators is identical wherever the entities live — and only core can
  weave family structure into its comorbidity seeding (core cannot import
  modules, so module-owned chart entities could never get first-class
  generation).
- **Stability after the transition.** Once tested, a basic chart rarely
  changes. Subsequent modules get a stable, versioned base; volatile or
  specialty concerns continue to arrive as extension modules through the
  registry, which loses none of its purpose (§3).

Alembic (built for exactly this moment) carries the migration; this is the
first real core schema evolution since the registry landed.

## 3. The thin-entity principle

Reference entities enter core as **identifier + name only** — nothing
more:

```
Provider   (id, npi_like_identifier, name)      # specialty via FK below
Specialty  (id, code, name)
```

Core needs their *identity* (a Visit points at a Provider); it does not
need their substance (credentials, schedules, institutions, panels). Later
modules extend these rows through the schema registry exactly as
`ontology_module` extended `Diagnosis` — columns and relationships added
declaratively, no core change. The rule generalizes: **core owns identity
and relationships; modules own richness.**

## 4. New and changed entities

New core tables (SQLAlchemy 2.0 `Mapped[]`, per house style):

| Entity | Key fields | Notes |
|---|---|---|
| `Provider` | identifier, name, specialty FK | thin (§3) |
| `Specialty` | code, name | thin (§3) |
| `FamilyMember` | patient FK, relationship (mother/father/sibling/child/spouse…), related_patient FK **nullable**, name, dob | when `related_patient` is set, the relative is another generated Patient — real synthetic families |
| `FamilyHistory` | patient FK, family_member FK nullable, relationship, condition description, icd10_code, concept FK nullable, onset_age | replaces the four booleans as source of truth |
| `Allergy` | patient FK, substance, reaction, severity (mild/moderate/severe), noted_date, concept FK nullable | replaces the pipe-separated string |
| `MedicationStatement` | patient FK, drug_name, drug_class, dose, frequency, status (active/completed/stopped), start/end dates, indication → ChronicCondition FK nullable | the cross-visit medication list; visit `Prescription` rows remain the *order* events that feed it |
| `Procedure` | patient FK, visit FK nullable, description, performed_date, provider FK nullable, concept FK nullable | the recording slot the SNOMED intervention work needs |
| `Immunization` | patient FK, vaccine name, cvx_code nullable, administered_date, dose_number | age-schedule generated |
| `VisitNote` | visit FK, note_type (soap/addendum), text, author → Provider FK, created_at | **the comprehension service's `NoteRecord` input, stored at generation time** |

Changed core tables:

- `Patient` +: `deceased` (bool), `deceased_date`, `marital_status`,
  `language`, `emergency_contact_name`, `emergency_contact_phone`.
  Retained-but-deprecated for one release: `allergies` text,
  `fam_hx_*` booleans (kept populated from the structured entities so
  existing consumers keep working — §7).
- `Visit` +: `provider` FK (nullable); `provider_name` string retained,
  deprecated.
- `ChronicCondition` +: `status` enum (active/resolved/remission,
  default active), `resolved_date`. `controlled` retained (it means
  something different — control of an *active* condition).

Insurance stays as the existing flat fields in v1 — structured
`Coverage` history is add-on territory, not basic chart.

## 5. Concept links

Every clinical entity carries a **nullable** `concept_id` FK into
`ontology_concepts` (the `Diagnosis.concept_id` pattern): FamilyHistory,
Allergy (substance), Procedure, Immunization, MedicationStatement (future
RxNorm), ChronicCondition (retrofit). Nullable is the contract: the chart
works with no catalog loaded; `hdh icd link` (and future `hdh snomed
link`) backfills. Core continues to know nothing about any ontology —
it holds foreign keys to a table whose *content* modules provide.

## 6. Generation: making the new chart real

The payoff section — every entity must be *medically realistic* or it is
dead weight:

- **Families first.** The generator builds households: shared surname and
  address, coherent ages (parents 20–40 years older than children),
  `FamilyMember` links both ways, some relatives as full generated
  patients. **Hereditary seeding gets honest:** today's
  `comorbidity_seeds(fam_hx booleans…)` becomes family-derived — a parent
  with generated T2DM produces the child's `FamilyHistory` row *and*
  raises the child's seeding probability. The flags stop being random;
  they become consequences.
- **Medication lists** derive from the existing prescription stream:
  chronic-condition drugs → `active` statements (indication FK set);
  acute courses → `completed`; occasional `stopped` with a switch to an
  alternative from the same formulary.
- **Immunizations** by age schedule (childhood series, annual flu for
  seniors/chronic patients, tetanus boosters) — deliberately simplified
  CDC-shaped schedule, seasonally timed like everything else.
- **Procedures** attach to conditions that imply them (laceration →
  repair; ankle fracture visit → reduction; wellness → screening
  colonoscopy by age) — modest v1 list from the existing profiles.
- **VisitNotes**: the narrative module's deterministic SOAP renderer
  *moves into core* as a plain-text note writer; every generated visit
  stores its note. The narrative module remains the presentation/LLM
  polish layer. (Resolves cleanly: the comprehension service's eval
  corpus (§8 of its design) becomes a first-class generated artifact.)
- **Providers**: a small generated practice (family physicians, NP/PA,
  a couple of specialists), assigned to visits with continuity — the
  same patient usually sees the same provider.

Determinism is preserved: same seed, same chart. Dataset shape changes,
so the shipped release DB is regenerated (v0.4.0).

## 7. Migration and back-compatibility

- **Alembic revision** (autogenerate from the new metadata): new tables +
  new nullable columns. Existing PostgreSQL datasets: `just db-upgrade`.
  Non-Alembic SQLite files: `create_all` adds new tables and
  `ensure_columns` adds nullable columns on open — old datasets stay
  readable; new entities are simply empty until regeneration.
- **Deprecated fields stay one release**, populated from the structured
  truth (`allergies` string rendered from `Allergy` rows; `fam_hx_*`
  derived from `FamilyHistory`), with removal in the release after —
  announced in release notes.
- `hdh migrate` (SQLite→PG) needs no change: metadata-driven.

## 8. Ripple effects across modules

| Module | Change |
|---|---|
| caregaps | family-hx-aware rules read `FamilyHistory`; immunization-gap rule becomes possible (flu shot overdue) |
| risk | new features: family-history burden, active-medication count from statements (replaces distinct-drug proxy), immunization currency |
| exporters / fhir_api | new resources: AllergyIntolerance, FamilyMemberHistory, MedicationStatement, Procedure, Immunization, DocumentReference (notes), Practitioner |
| narrative | renderer relocates to core (writer); module keeps presentation + `--llm` polish |
| agent | schema summary + tools see the new tables; "does anyone in her family have heart disease?" becomes answerable |
| comprehension (future) | `VisitNote` is its input; its `NoteMention` output FKs to chart entities |
| icd10cm / snomed | more link targets (`hdh icd link` covers FamilyHistory + Procedure) |

## 9. Testing

Existing suite regenerates in memory, so behavioral drift surfaces
immediately. New: family-coherence properties (ages, bidirectional links,
hereditary seeding correlation actually present), medication-list
consistency with prescriptions, note-per-visit invariant, migration test
(pre-expansion DB opens and upgrades — the `test_existing_database_upgrade_path`
pattern), FHIR round-trips for each new resource. Statistical snapshot of
the regenerated dataset goes in the release notes.

## 10. Phases

| | Delivers |
|---|---|
| **A** | models + Alembic revision + migration tests (chart exists, empty) |
| **B** | generators: families/hereditary seeding, providers, notes-in-core |
| **C** | generators: medication statements, immunizations, procedures, allergies |
| **D** | exporters + FHIR API + module ripples (§8) |
| **E** | deprecation wiring, docs, regenerate + release v0.4.0 |

## 11. Review decisions (questions resolved 2026-08-11)

1. **Family depth — mixed.** Household members (parents/children living
   together) are full generated patients; extended relatives are
   lightweight `FamilyMember` rows carrying a narrative `summary`
   ("father lived to 75, diabetes/hypertension mostly well managed, died
   of natural causes") plus structured `FamilyHistory` rows.
2. **No deprecation window.** `fam_hx_*` booleans, the `allergies`
   string, and `Visit.provider_name` are **removed outright** — the
   project is early enough (<100 clones) that a clean break beats a
   compatibility shim. Release notes carry the announcement.
3. **Notes are deterministic by default**; the LLM-polished variant is
   stored only when generation is explicitly run with `--llm`.
4. **Unified problem list — now.** One `Condition` table replaces BOTH
   `ChronicCondition` and visit-level `Diagnosis`: patient FK, optional
   visit FK (where recorded), icd10_code, description, `chronic` flag,
   status (active/resolved/remission), `controlled` (chronic only),
   onset/resolved dates, is_primary. Cleaner while the model is still
   evolving; registry extensions (snomed columns, concept link) re-target
   Condition.
5. **Emergency contact is a `FamilyMember` FK** — the relationship
   entity will serve that role for a long time.
