# Specialty Modules, and Measuring Against Published Standards (Draft)

**Where it lives:** a new specialty module (acute stroke is the worked
example) · `hdh.core` grows three things ·
**Follows:** [rxnorm-and-terminology-boundaries.md](rxnorm-and-terminology-boundaries.md) §3 (what belongs where) ·
**Status:** DRAFT · **Date:** 2026-08-25

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements; the capture-first thesis, and the framing that a benchmark is a definition rather than an integration |
| | | |

Every module so far has extended hdh in **one** direction. `icd10cm` and
`snomed` added vocabularies. `comprehension` added a pipeline.
`interchange` added a boundary. None of them added a clinical domain with
its own encounter shape, its own quality standard, and its own idea of
what "doing well" means.

This document designs the one that does, and it is deliberately two
things at once:

1. **An architecture test.** Can a module extend the schema *and* the
   functionality extensively, and still leverage core rather than
   forking it? Where does core have to grow, and where should it refuse?
2. **A capability.** Can hdh answer *"how is this service performing
   against the international standard?"* — with dashboards and metrics —
   **without an integration project?**

**The thesis, in one line:** a published quality standard is a set of
**definitions**, not a system to integrate with; if the chart is already
coded, a benchmark is a query plus a definition.

---

## Contents

1. [What the existing extension points reach — and what they don't](#1-evidence)
2. [A benchmark is a definition, not an integration](#2-thesis)
3. [The worked example: an acute care pathway](#3-pathway)
4. [Contracts: events, scores, and eligibility](#4-contracts)
5. [Measures have three shapes](#5-measures)
6. [Where core has to grow](#6-core)
7. [Agent-first: dashboards without a dashboard](#7-agent)
8. [Ownership: module or core](#8-ownership)
9. [Milestones](#9-milestones)
10. [Open questions](#10-questions)

---

## 1. What the existing extension points reach — and what they don't<a name="1-evidence"></a>

Core publishes five discovery hooks. A specialty module needs all five,
and then runs out:

| Hook | What it extends | Enough? |
|---|---|---|
| `SCHEMA_MODULES` | entities and columns, declaratively | ✅ |
| `GENERATOR_MODULES` | condition packs and synthetic data | ✅ (empty today — this would be its first user) |
| `ONTOLOGY_MODULES` | vocabularies | ✅ |
| `FHIR_MODULES` | export emitters and enrichers | ✅ |
| `CLI_MODULES` | subcommands | ✅ |
| — care-gap finders | protocol surveillance | ❌ **no hook** |
| — aggregate measures | rates, intervals, distributions | ❌ **does not exist** |
| — sites | comparing one service against another | ❌ **no entity** |

**The finder gap is the sharpest.** `GapFinder` is already a proper
protocol with a `FINDERS` registry — the design is right. But the
registry is a dict inside `caregaps`, populated by its own imports, so a
specialty module cannot register a finder without `caregaps` importing
it. That inverts the dependency rule the project holds everywhere else:
modules use each other's **public API**, never each other's internals,
and core never imports a module at all.

This is the same shape as every previous extension point in this
codebase. `termsearch` was extracted when LOINC became the funnel's
second consumer. `ConditionSource` appeared when a second condition pack
did. **A registry becomes a hook when something outside wants in** — and
this is that moment for care-gap detection.

## 2. A benchmark is a definition, not an integration<a name="2-thesis"></a>

The conventional route to answering *"are we meeting the standard?"* is:
implement an EHR, employ someone to abstract charts into a registry's
format, submit, and receive a report months later. The cost is
overwhelmingly **abstraction labour** — a person reading free text and
re-typing it into a form.

That labour exists because the record is prose and the standard wants
structure. hdh removes it from the other end: the chart is *already*
coded to SNOMED CT, ICD-10-CM, LOINC and RxNorm, because comprehension
coded it when the note was written.

So the standard is not a system to connect to. **The standard is its
definitions**, and those are published:

- a **numerator** — what counts as the thing being done
- a **denominator** — who should have had it done
- **exclusions** — who is legitimately not counted
- a **target** — the value that counts as meeting it

Nothing in that list is an API. It is a specification, and specifications
can be written down as data.

Two consequences worth being explicit about:

**The definitions are the integration.** Fidelity to a published measure
set is the *only* thing that makes numbers comparable — to another
service, to a registry, to last quarter. A measure that is *nearly* the
standard's is worse than no measure, because it looks comparable and
isn't. So measure definitions cite their source and version, and the
working assumption is that we **transcribe published definitions rather
than invent our own**.

**This does not make the numbers valid.** hdh runs on synthetic data.
What this design demonstrates is that the *architecture* can produce
them; whether a real deployment's numbers are trustworthy depends
entirely on capture quality, which is a different problem and an honest
one to name.

## 3. The worked example: an acute care pathway<a name="3-pathway"></a>

Acute stroke is the example throughout, because it exercises everything
a specialty module can exercise: a different encounter shape, time-
critical processes, severity instruments, eligibility logic, and a
mature published standard (AHA/ASA *Target: Stroke*, WSO/ESO *Angels*
quality indicators) whose definitions are freely available.

Nothing below is stroke-specific in the *architecture*. Sepsis, STEMI,
trauma and maternity all have the same shape: a pathway with timed
steps, a denominator of eligible patients, and a published target.

### What is structurally new

**The encounter is an admission, not a visit.** hdh's `Visit` is an
outpatient contact with a date. A pathway encounter has an arrival
instant, a sequence of timed events, and a disposition.

**The interval *is* the measure.** Nobody records "door-to-needle time".
They record when the patient arrived and when treatment started; the
measure is the difference. This is the single most important structural
fact in this document, and it decides the schema:

> The chart records **events with timestamps**. Measures compute
> intervals. An interval is never stored as a fact, because a stored
> interval cannot be re-derived, corrected, or audited against its
> endpoints.

**Severity is an instrument, not an opinion.** Assessment scales are
LOINC-coded observations with a value and a time — which means they are
`LabResult`-shaped, not `Condition`-shaped, and §10.0 applies to them
unchanged: a note may *refer* to a score; a recorded score comes from
someone administering the instrument.

**Eligibility is computed and contested.** *"Thrombolysis rate among
eligible patients"* requires knowing who was eligible — a function of
time since onset, contraindications, and clinical judgement. This is the
hardest part of the design and §10 Q6 asks how to represent it.

## 4. Contracts: events, scores, and eligibility<a name="4-contracts"></a>

Three new entity shapes, all owned by the module, all extending core
through `SCHEMA_MODULES`.

### 4.1 `PathwayEncounter`

An episode of care governed by a protocol. Carries the arrival instant,
the pathway it is on, the site it happened at, and its disposition. It
**references** a `Visit` rather than replacing it, so the rest of hdh —
exports, the agent, the chart view — keeps working without knowing
pathways exist.

### 4.2 `PathwayEvent`

One timestamped step. `kind` comes from a closed vocabulary the pathway
defines (arrival, imaging performed, decision made, intervention
started, disposition). Carries a time, an actor, and optionally the
`ServiceRequest` or `Procedure` it corresponds to.

Deliberately **not** a free-text audit log: a measure has to find "the
imaging event" reliably, so the kind is a closed set and a typo is a
load error rather than a silently missing numerator.

### 4.3 `AssessmentScore`

An instrument result: which instrument (LOINC), value, time, who
administered it. Distinct from `LabResult` because there is no specimen
and no reference range — the same reasoning §10.0 used to keep prose
values out of the lab table applies here in the other direction.

### What is reused, not rebuilt

| Need | Existing core/module surface |
|---|---|
| the drug given | `Prescription` + RxNorm coding |
| the imaging or procedure ordered | `ServiceRequest` (kind `procedure`) |
| the diagnosis | `Condition` + SNOMED/ICD |
| who did it | `Provider` |
| the note it came from | `VisitNote` + comprehension |
| corrections | `hdh.core.chartedit` — amend/void with a reason |

A specialty module that re-invented any of these would be the failure
this document is testing for.

## 5. Measures have three shapes<a name="5-measures"></a>

Every measure in a published set is one of three, and the contract has
to carry all three without collapsing them into "a number":

**Rate** — a proportion. *Anticoagulation among patients with atrial
fibrillation at discharge.* Needs numerator, denominator, exclusions.

**Interval** — an elapsed time against a threshold, reported as a
proportion **and** a distribution. *Door-to-needle within 60 minutes in
≥85% of treated patients.* A median alone hides the tail, and the tail is
where the harm is; the target itself is written as "X% within Y", so the
contract must express that shape natively.

**Distribution** — a spread compared to a reference. *Modified Rankin
Scale at 90 days.* There is no single number; the comparison is the
shape against a benchmark's shape.

### Definitions are data

A measure definition should be **declarative** — the same choice the
schema registry already makes for entities:

```
id            the standard's own identifier
source        which published set, and which version
numerator     what counts as done
denominator   who should have had it
exclusions    who is legitimately not counted
target        the value that counts as meeting it
```

Three reasons this is data rather than code:

1. **It is citable.** A number is only comparable if its definition is,
   so the definition has to travel with the result.
2. **Standards revise.** A version bump should be a data change with a
   visible diff, not a code change buried in a function.
3. **It can be validated.** A definition naming a `PathwayEvent.kind`
   that does not exist should fail at load, exactly as a bad schema
   extension does today.

### The result carries its own evidence

A measure result is not a float. It carries the numerator count, the
denominator count, the excluded count **with reasons**, the definition
id and version, the period, and the site. Anything less is a number
nobody can check — and the project's whole posture is that an unverifiable
confident answer is worse than a refusal.

The corollary: a measure whose denominator cannot be computed **reports
that it cannot**, rather than returning zero. A zero and an unknown look
identical in a dashboard and mean opposite things.

## 6. Where core has to grow<a name="6-core"></a>

Three additions, and the argument for each being core rather than module.

### 6.1 A site dimension

Comparing one service against another is the entire point of a
benchmark, and hdh has `Provider` with nothing above it. A `Site` (or
organisation) entity is not specialty-specific — every module wants it,
the agent's population questions want it, and #88's dashboards want it.
**Core.**

The migration question is what an existing single-site dataset becomes;
§10 Q1 asks it.

### 6.2 A finder discovery hook

`caregaps` publishes `GapFinder` and a registry. It needs the registry to
be *reachable from outside*, the way `ONTOLOGY_MODULES` and
`FHIR_MODULES` already are — a module names its finder, core discovers
it, `caregaps` never imports a specialty.

Small change; unblocks protocol-compliance surveillance without any
module reaching into another.

### 6.3 A measure contract and runner

Aggregate measurement does not exist anywhere in hdh today, and it is not
neurology's to own. The **contract** (definition, result, the three
shapes) and the **runner** belong in core; the **definitions** belong to
whoever transcribes a standard.

This is exactly the `termsearch` split: the mechanism is shared, the
profile is the caller's.

## 7. Agent-first: dashboards without a dashboard<a name="7-agent"></a>

**No fixed UI for this module.** The same commitment as everywhere else:
the agent is the interface, and a screen is one renderer among several.

A question like *"how are we doing on door-to-needle this quarter, by
site?"* resolves as: the agent selects a measure definition, runs it
through the core runner, and returns a **typed result**. #88's chart
specification renders it; the terminal prints it; an MCP client (#90)
receives it as structured data.

Three constraints, all inherited rather than invented:

**A measure is not an agent-only decision** (design §7). `hdh measures
run` must reach the identical computation. If a dashboard can only be
obtained by asking, the decision has leaked into a front door.

**The agent selects, it does not define.** The model picks *which*
published measure answers the question and over what period and site. It
does not author a numerator. A model-invented measure would be a number
with no standard behind it, which is precisely the thing this design
exists to avoid — and it is the same split as everywhere else: **the LLM
classifies, deterministic code decides.**

**Ungrounded aggregates are the new hallucination surface.** The response
validator checks claims against tool evidence, and a measure result
carrying its counts and definition id is exactly the evidence it needs.
A rate quoted without them should not survive validation.

## 8. Ownership: module or core<a name="8-ownership"></a>

| Owned by the specialty module | Owned by core |
|---|---|
| pathway schema (`PathwayEncounter`, `PathwayEvent`, `AssessmentScore`) | the `Site` dimension |
| the closed event vocabulary for its pathways | the measure contract and runner |
| transcribed measure definitions + their source and version | the finder discovery hook |
| eligibility rules | `chartedit`, `ServiceRequest`, `Condition`, terminology modules |
| its protocol-compliance finder | the `GapFinder` protocol itself |
| its condition pack and synthetic generation | `ConditionSource`, the generator |
| its agent tools | the agent, its pipeline and its guardrails |

The test this document is really running: **if building the specialty
module requires changing anything in the right-hand column beyond the
three additions in §6, the architecture did not hold.**

## 9. Milestones<a name="9-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **M1** | Core: the `Site` dimension, and the finder discovery hook. No specialty code yet. | a module can register a care-gap finder without `caregaps` knowing it exists |
| **M2** | Core: the measure contract — definitions as data, results carrying their evidence, the three shapes — and a runner. Validated at load. | a measure definition naming a missing field fails loudly |
| **M3** | The specialty module: pathway schema, closed event vocabulary, `AssessmentScore`, a fabricated fixture, and a condition pack generating synthetic pathway encounters | `GENERATOR_MODULES` gets its first real user; the chart still exports and reads correctly without the module loaded |
| **M4** | Transcribed measure definitions from a published set, with eligibility rules and their exclusions | a rate, an interval and a distribution all compute — and an uncomputable denominator reports itself rather than returning zero |
| **M5** | The agent surface: measure selection, results as structured evidence, and the same computation via `hdh measures run` | a benchmark question answered in conversation, and the same number obtained without the agent |

Each milestone is human-tested before the next begins.

## 10. Open questions<a name="10-questions"></a>

**Q1 — Does the `Site` dimension go in core, and what happens to existing
data?** The argument for core is in §6.1. If yes: does an existing
single-site dataset get a synthesised default site, or does the column
stay nullable and measures report "unattributed"?

**Q2 — Generated, ingested, or both?** A condition pack producing
synthetic pathway encounters makes everything testable end to end and
gives `GENERATOR_MODULES` its first user. The alternative is ingest-only,
which is closer to a real deployment but leaves the module untestable
without hand-built fixtures.

**Q3 — Are measure definitions data or code?** §5 argues data, for
citability and versioning. The cost is a small definition language and a
validator. Is that worth it, or do definitions start as Python and move
to data when a second standard arrives — the pattern this codebase has
used successfully three times?

**Q4 — Does a protocol-compliance finding reuse `CareGap`?** A missed
dysphagia screen and an overdue mammogram are both "something that
should have happened and didn't", so `CareGap` may fit as-is. But
`gap_type` is currently a free string and severity is a three-value
guess; a protocol deviation has a *definition* behind it and possibly a
time target. Extend, or a sibling type?

**Q5 — Where do timed events live?** §4.2 proposes `PathwayEvent` as a
first-class entity. The alternative is timestamps on the existing
`ServiceRequest` and `Procedure` rows, which avoids a new entity but
scatters the pathway across three tables and makes "the imaging event"
harder to find reliably.

**Q6 — How is eligibility represented?** Computed on demand from chart
state, or recorded as a decision with its evidence at the time? Computed
is simpler and always current. Recorded is honest about clinical
judgement, survives later chart changes, and is what a human would want
to see when asked why a patient was excluded — but it is a new entity and
a new capture burden.

**Q7 — Is this one specialty module, or a general pathway capability?**
The pathway machinery, the measure runner and the finder hook are not
specialty-specific; only the event vocabulary, the definitions and the
eligibility rules are. Should the module be `hdh.modules.pathways` with
stroke as a *pack* — mirroring `ConditionSource` — or a standalone
specialty module that a second specialty later forces us to generalise?
The codebase's own history argues for the second: `termsearch` and
`ConditionSource` were both extracted **after** a second consumer
existed, and generalising from one case is the mistake the RxNorm
document was written to correct.
