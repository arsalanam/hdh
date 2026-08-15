# Clinical Breadth & Pluggable Condition Packs — Design (Draft)

**Target:** `hdh.core` (disease engine) · **Status:** IMPLEMENTED (milestones A–C, 2026-08-14) ·
**Date:** 2026-08-14 · **Issue:** [#28](https://github.com/arsalanam/hdh/issues/28)

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, design principles, review |
| | | |

The SNOMED-powered agent found the gap on its first real cohort query:
every cardiovascular patient carries exactly one condition — I10. The
*symptom* is a narrow catalog; the *cause* is a closed one. Adding a
condition today means hand-editing four parallel structures
(`CONDITIONS`, `AGE_WEIGHTS`, `comorbidity_seeds`, and generators.py's
`CHRONIC_NAMES`), so the catalog stopped growing. This design fixes the
structure first, then uses the fixed structure to add the missing
clinical content — and makes future packs (specialty sets, alternative
generators) pluggable with zero core edits.

---

## Contents

1. [What exists, and where it violates the principles](#1-what-exists)
2. [Contracts: strong, immutable types](#2-contracts)
3. [The catalog service: encapsulation + DI](#3-catalog-service)
4. [Pluggability: condition packs](#4-condition-packs)
5. [Comorbidity links and progressive onset](#5-comorbidity)
6. [The cardiometabolic pack (the #28 payload)](#6-cardiometabolic-pack)
7. [Migration and compatibility](#7-migration)
8. [Testing](#8-testing)
9. [Milestones](#9-milestones)
10. [Open questions](#10-open-questions)

---

## 1. What exists, and where it violates the principles<a name="1-what-exists"></a>

| Today | Principle violated |
|---|---|
| `ConditionProfile`/`LabSpec`/`RxSpec` are **mutable** dataclasses with bare `list`/`dict`/`tuple` fields | immutability; strong types |
| Chronicity lives in **generators.py** (`CHRONIC_NAMES`), hereditary keys in a second dict, the profile knows neither | single responsibility; explicit contracts |
| `CONDITIONS` is a module-global dict consumed raw by generators, cli, exporters | encapsulation — no service boundary, internals are the API |
| `pick_condition`/`comorbidity_seeds` use **module-global `random`** | dependency injection; blocks the deferred per-household-seed parallelism plan |
| `comorbidity_seeds` is imperative if/else — day-1 seeding only, no cross-condition risk (HTN never *causes* CKD), onset dates carry no clinical ordering | extensibility; the actual clinical gap |
| Adding one condition touches 4 structures in 2 files | the reason the catalog froze at 32 |

## 2. Contracts: strong, immutable types<a name="2-contracts"></a>

All engine types become **frozen** dataclasses with fully typed fields
(`tuple[LabSpec, ...]`, not `list`). The profile absorbs what belongs to
it:

```python
@dataclass(frozen=True)
class ConditionProfile:
    name: str                      # the catalog key, now ON the object
    icd10_code: str
    snomed_code: str | None        # authored alongside ICD (feeds issue #29)
    description: str
    chief_complaint: str
    visit_type: VisitType          # the enum, not a str
    chronic: bool                  # absorbs generators.CHRONIC_NAMES
    hereditary_key: str | None     # absorbs generators.HEREDITARY_KEYS
    vitals: VitalsDeltas           # frozen sub-struct of (mean, sd) pairs
    labs: tuple[LabSpec, ...]
    rx_options: tuple[RxSpec, ...]
    rx_pick_all: bool
    follow_up_days: int | None
    seasonal_weights: SeasonalWeights   # immutable Mapping wrapper
    onset: OnsetProfile | None     # §5 — None for acute conditions
```

New declarative types (§5): `OnsetProfile`, `RiskModifier`,
`ComorbidityLink`. Age bands become an `AgeBand` enum (no magic
strings). Every collection exposed by the engine is a tuple or a
read-only mapping — a consumer *cannot* mutate the catalog.

## 3. The catalog service: encapsulation + DI<a name="3-catalog-service"></a>

One public surface replaces the module globals:

```python
class ConditionCatalog:
    """Sampling + lookup over an immutable set of ConditionProfiles.
    Internals (_profiles, _band_pools) are private; consumers never
    see a raw dict."""

    def get(self, name: str) -> ConditionProfile: ...          # KeyError = loud
    def names(self) -> tuple[str, ...]: ...
    def chronic(self) -> tuple[ConditionProfile, ...]: ...
    def sample_visit_condition(self, ctx: SamplingContext) -> ConditionProfile: ...
    def seed_chronic(self, ctx: SamplingContext) -> tuple[ConditionProfile, ...]: ...
    def annual_onsets(self, ctx: SamplingContext) -> tuple[ConditionProfile, ...]: ...  # §5
```

```python
@dataclass(frozen=True)
class SamplingContext:
    """Every input sampling needs, made explicit — nothing global."""
    age: int
    sex: Sex
    month: int
    established: frozenset[str]
    family_history: frozenset[str]      # hereditary keys present
    smoker: bool
    bmi: float
    rng: random.Random                  # INJECTED — deterministic tests,
                                        # per-household seeds later (Celery plan)
```

`build_dataset(session, ..., catalog: ConditionCatalog | None = None)` —
the generator receives the catalog; tests inject small ones; `None`
means "the default assembly" (§4). The injected `rng` flows from
`build_dataset`'s master seed, closing the global-`random` hole.

## 4. Pluggability: condition packs<a name="4-condition-packs"></a>

The `GapFinder`/`LoadStage`/`OntologyService` move, applied once more:

```python
class ConditionSource(Protocol):
    """One pack of authored conditions (a specialty set, a locale set,
    a research scenario...)."""
    name: str
    def conditions(self) -> tuple[ConditionProfile, ...]: ...
```

- Core ships two packs: **`family-medicine-core`** (today's 32,
  restructured, behavior-preserving) and **`cardiometabolic`** (§6).
- `build_catalog(sources: Iterable[ConditionSource]) -> ConditionCatalog`
  merges packs. **Duplicate condition names are a hard error** — clinical
  content must not silently override (deliberately stricter than the
  schema registry's later-wins, which suits columns, not medicine).
- Future feature modules contribute packs through a
  **`GENERATOR_MODULES`** discovery tuple in `hdh.modules` (exactly like
  `CLI_MODULES`/`FHIR_MODULES`/`ONTOLOGY_MODULES`), resolved at the
  composition root (`hdh.cli` / `build_dataset` default) — core never
  imports modules; a neurology or oncology pack lands with zero core
  edits.

## 5. Comorbidity links and progressive onset<a name="5-comorbidity"></a>

The clinical heart of the design. Today's imperative `comorbidity_seeds`
becomes declarative data on each chronic profile:

```python
@dataclass(frozen=True)
class OnsetProfile:
    """When and why this chronic condition begins."""
    base_annual_rate: float                     # per year at risk, age-eligible
    min_age: int
    sex_factor: Mapping[Sex, float]             # default 1.0/1.0
    modifiers: tuple[RiskModifier, ...]          # smoker, bmi≥x, family history
    comorbid_links: tuple[ComorbidityLink, ...]  # THE webs

@dataclass(frozen=True)
class ComorbidityLink:
    condition: str          # e.g. "hypertension"
    relative_risk: float    # multiplies the annual rate while established
```

Two-phase seeding replaces day-1-only:

1. **Baseline seeding** (chart start): as today, but computed *from the
   OnsetProfiles* (cumulative risk for the patient's age), not from
   hardcoded if/else.
2. **Annual onset rolls** during history generation: each simulated
   year, `annual_onsets(ctx)` evaluates not-yet-established chronic
   conditions with relative risk from what IS established. CKD now
   appears *after* years of hypertension, with an onset date that
   post-dates it — cohort queries return clinically ordered charts, and
   "no cerebrovascular disease despite hypertension" (the agent's own
   observation) stops being structurally guaranteed.

Sampling stays a pure function of `SamplingContext` — reproducible,
testable offline, no LLM, no I/O.

**Amendment (review decision, §10 Q1): severity staging ships in
milestone B, not deferred.** A chronic profile may declare a
`StageProfile`: an ordered tuple of stages (each with its own ICD-10
code, e.g. CKD N18.30 → N18.31 → N18.4) and per-period probabilities of
progressing, improving, or holding. Stages are evaluated on a
**configurable cadence** (`--progression-cadence yearly|quarterly`,
default yearly) during history generation, so a 4-year chart shows
disease trajectories instead of a frozen stage. Stage changes update the
patient's Condition row code and are visible in onset-ordered history.

**Amendment (implementation note): vitals deltas stay flat.** The
generator consumes `bp_sys_delta[0]`-style fields directly; the frozen
profile keeps those field names as `tuple[float, float]` instead of a
`VitalsDeltas` sub-struct — same information, same immutability, fewer
touch points.

## 6. The cardiometabolic pack (the #28 payload)<a name="6-cardiometabolic-pack"></a>

Eight conditions, each authored with ICD-10 **and** SNOMED codes, drug
and lab profiles, follow-up cadence, and comorbidity links. Prevalence
targets are *plausible for a family-medicine panel*, not epidemiological
claims — recorded as sanity-test ranges, not assertions of truth:

| Condition | ICD-10 | Driven by (RR links) | Care pattern |
|---|---|---|---|
| Chronic kidney disease, stage 3 | N18.30 | HTN ×3, T2DM ×3, age | creatinine/eGFR panel, ACE inhibitor, q3–6mo follow-up |
| Coronary artery disease | I25.10 | HTN ×2, hyperlipidemia ×2.5, smoking ×2, T2DM ×1.8 | statin + aspirin + beta-blocker, lipid panel |
| Heart failure (chronic) | I50.32 | CAD ×4, HTN ×2 | BNP lab, loop diuretic + ACEi + BB, q3mo follow-up |
| Atrial fibrillation | I48.91 | age-driven, HTN ×1.8, HF ×2 | anticoagulant (apixaban; warfarin+INR variant), rate control |
| Stroke/TIA history | Z86.73 | AFib ×4, HTN ×2, CAD ×1.5 | antiplatelet, risk-factor management |
| Asthma | J45.909 | youth-onset, seasonal | SABA + ICS, spirometry note, seasonal weights |
| Chronic anemia (iron def.) | D50.9 | CKD ×2, female factor | CBC + ferritin, iron supplement |

Per §10 Q5 (decided): CKD arrives **additively** — the patient's
existing E11.9 row is never rewritten; the E11.22 combined code is a
possible future refinement.

Plus catalog metadata updates: `RELATIVE_CONDITIONS` (family-history
narratives) gains CAD/stroke/CKD entries with hereditary keys wired to
the new onset modifiers, so hereditary seeding reaches the new pack.

## 7. Migration and compatibility<a name="7-migration"></a>

All `disease_engine` consumers are repo-internal (generators, cli
`list-conditions`, exporters, core `__init__`). **Full migration, no
shims**: `CONDITIONS` / `AGE_WEIGHTS` / `pick_condition` /
`comorbidity_seeds` / `CHRONIC_NAMES` are deleted, callers move to the
catalog API. The public dataset contract (schema, CLI flags) is
unchanged; regenerated datasets differ (richer, as intended). The
quality gate's immutability check starts passing on the engine instead
of warning about its mutable constants.

## 8. Testing<a name="8-testing"></a>

- **Behavior preservation** (milestone A): the restructured
  family-medicine-core pack, sampled with a fixed seed, produces the
  same condition-name distribution as today within tolerance (χ² sanity,
  not byte equality — the RNG plumbing changes).
- **Statistical properties** (milestone B): on a 500-patient seeded run —
  senior HTN prevalence within its target range; ≥80% of CKD patients
  have antecedent HTN or T2DM; every CKD onset date ≥ its antecedent's;
  AFib patients carry an anticoagulant; no condition name occurs twice
  in a catalog build (hard-error test).
- **Pluggability**: a throwaway test pack merges via `build_catalog`;
  duplicate name raises; `GENERATOR_MODULES` discovery mirrors the
  existing module-hook tests.
- **Determinism**: same master seed ⇒ identical dataset (now provable —
  injected RNG).

## 9. Milestones<a name="9-milestones"></a>

| | Delivers | Proves | Status |
|---|---|---|---|
| **A** | frozen contracts, `ConditionCatalog`, `SamplingContext`, injected RNG, family-medicine-core pack; all callers migrated | behavior-preserving restructure; principles hold before content grows | ✅ done |
| **B** | `OnsetProfile`/`ComorbidityLink`, two-phase seeding, **severity staging (`StageProfile`, configurable cadence)**, the cardiometabolic pack, seeded determinism + statistical tests | the webs are real; charts show trajectories; the agent's cohort query returns a mixed population | ✅ done (determinism = run-level seed: global RNG + Faker + catalog RNG seeded together; per-callsite injection lands with the Celery per-household arc) |
| **C** | `GENERATOR_MODULES` discovery + docs (`docs/guides/core.md`, README disease coverage) + `hdh list-conditions` by pack | zero-core-edit extension proven with a test pack | ✅ done |

## 10. Open questions<a name="10-open-questions"></a>

1. **Onset realism vs. simplicity**: annual onset rolls (proposed) give
   ordered onset dates at modest complexity. Enough, or should severity
   staging (CKD 3→4, HF class) come now? *Lean: defer staging.*  It should come now as
   we generate several years data so if we generate 4 years data it does not make sense that
   all CKD patients are stuck at stage 3 for 4 years..for now we can do yearly randomization of progression or
   improvemnt , but it should be configurable (quarterly , yearly)
2. **Prevalence calibration**: plausible-for-a-panel ranges (proposed)
   vs. literature-cited rates per condition. *Lean: plausible + honest
   labeling; hdh is a sandbox, not an epidemiology model.* Pausible is good
3. **SNOMED codes on profiles**: authoring them now (proposed) partially
   pre-empts issue #29 for new conditions; old 32 get codes
   opportunistically. Acceptable overlap? yes
4. **Pack granularity**: one `cardiometabolic` pack (proposed) vs.
   per-system packs (cardio / renal / respiratory) from day one.
    one for now ability to add more later
5. **E11.22 swap**: replacing a patient's E11.9 with E11.22 when CKD
   arrives touches condition-history semantics (same disease, new code).
   v1 could instead just add CKD alongside untouched E11.9. *Lean: the
   simple additive version first.*  yes it is ok

