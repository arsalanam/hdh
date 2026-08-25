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
6. [A worked example at scale](#6-worked)
7. [Where core has to grow](#7-core)
8. [Agent-first: dashboards without a dashboard](#8-agent)
9. [Ownership: module or core](#9-ownership)
10. [Milestones](#10-milestones)
11. [Open questions](#11-questions)

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
hardest part of the design and §11 Q6 asks how to represent it.

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

## 6. A worked example at scale<a name="6-worked"></a>

Take a hypothetical that makes the design concrete: **ten stroke centres
built across a country of over two hundred million people**, funded
externally, three or four years in. The funders want to know whether the
investment changed anything — not whether the buildings exist, but
whether **acute reperfusion, thrombectomy and rehabilitation are
happening, and whether mortality and morbidity moved.**

That question is harder than it sounds, and the interesting part of this
section is *why* — because the architecture has to be honest about it
rather than produce a confident chart.

### 6.1 The question they ask is not the question that can be answered first

Funders ask about **outcomes**. Outcomes are the right thing to care
about and the wrong thing to start with:

| | Process measures | Outcome measures |
|---|---|---|
| e.g. | door-to-needle, dysphagia screen before intake, thrombectomy rate among eligible | 90-day mortality, modified Rankin at 90 days |
| feedback loop | same day | 90 days minimum |
| confounded by case mix | barely | **severely** |
| actionable | directly — a centre can change it next week | only through the processes that drive it |
| N needed to see a change | small | large |

A centre that receives sicker patients will look worse on mortality
while delivering better care. So an outcome comparison between centres is
**meaningless without case-mix adjustment**, and a dashboard that ranks
ten centres by raw mortality is actively harmful — it punishes the
centres taking the hardest cases.

The architecture's answer is not to choose. It is to measure **process
continuously and outcome periodically**, state which is which, and never
present an unadjusted outcome comparison as a ranking.

### 6.2 What makes adjustment possible is the coded chart

Case-mix adjustment needs baseline severity, age, comorbidity burden and
time from onset to arrival. Every one of those is already in an hdh
chart:

| Adjustment variable | Where it already lives |
|---|---|
| baseline severity | `AssessmentScore` — the admission instrument, LOINC-coded (§4.3) |
| age, sex | `Patient` |
| comorbidity burden | `Condition` — the problem list, SNOMED/ICD-coded |
| onset-to-arrival | `PathwayEvent` timestamps (§4.2) |
| pre-morbid function | `AssessmentScore` — the baseline functional score |

This is the payoff of coding at write time. The variables that make an
outcome comparison defensible are the same variables comprehension
already extracts from the note — so adjustment is a query, not a second
data-collection programme.

Without that, a funder's outcome question requires a research study. With
it, it requires a definition.

### 6.3 The three programmes, and what each actually measures

**Acute reperfusion.** Thrombolysis rate among eligible patients, and
door-to-needle interval. Eligibility is the whole difficulty (§10 Q6):
the denominator is *"patients who could have been treated"*, which is a
clinical judgement about onset time and contraindications, not a field.
A centre can improve its rate by treating more patients **or** by
recording fewer as eligible, and only one of those is progress. The
exclusion counts and their reasons are therefore not a footnote — they
are the measure's integrity.

**Thrombectomy.** Door-to-device, split by direct arrival and transfer,
because a transferred patient's clock includes another hospital's delay
and mixing them hides where the time went. At ten centres this is also
where **volume** bites: a centre performing a small number of procedures
per quarter cannot produce a stable interval percentile, and reporting
one anyway invents precision. A measure result that carries its
denominator makes this visible; one that reports only a percentage does
not.

**Rehabilitation.** Three distinct things that get conflated: whether an
assessment happened, how long it took, and whether function improved.
The first two are process. The third is a **paired** measure — a
functional score at discharge and another at follow-up — and it is the
only one that speaks to morbidity. It needs both scores on the same
patient, which is a capture requirement, not a computation.

### 6.4 The threat to every outcome claim: follow-up completeness

A 90-day outcome exists only if someone captured it at 90 days.

If a third of patients are lost to follow-up, and the lost patients are
systematically different — sicker, poorer, further away — then measured
outcomes are better than real outcomes, by an unknown margin and in a
consistent direction. This is the single largest threat to an
outcome-based programme, and it is invisible in any dashboard that
reports only the patients it has.

So **follow-up completeness is itself a first-class measure**, reported
beside every outcome it conditions:

> 90-day functional independence: 48% *(of 213 patients with a recorded
> 90-day assessment, from 341 eligible — **62% follow-up**)*

The parenthetical is not a caveat. It is the number that tells a reader
whether to believe the first one. A measure result that carries its
denominator and its exclusions (§5) produces this by construction, which
is the point of that contract.

### 6.5 What the centres describe — and what they do not

Ten centres in a country of that size see a small fraction of its
strokes. Their numbers describe **the patients who reached them**, not
the national burden, and the two get conflated constantly in reporting.

That distinction has to survive into the output. A measure knows its
denominator, and the denominator is *presented patients*, not
*population*. Any population-level claim needs a catchment denominator
hdh does not have and should not pretend to — the honest report says
*"among patients presenting to these centres"* and stops.

The same discipline as everywhere else in this project: the system
reports what it can support and declines the rest, rather than producing
a plausible number nobody can check.

### 6.6 Tracking change over time

*"Did it improve?"* is a comparison of the same definition across
periods, which is only sound if three things hold:

1. **The definition did not change.** Hence versioned definitions (§5) —
   a revised measure produces a discontinuity that looks like a real
   change and is not.
2. **Capture did not change.** A centre that starts recording severity
   scores properly will appear to get sicker patients and better
   risk-adjusted outcomes simultaneously. Capture completeness is
   reported alongside, for the same reason follow-up is.
3. **The comparison is powered.** Ten centres, quarterly, is a small
   number of events per cell for an outcome measure. Process measures
   support quarterly comparison; outcome measures may support annual.
   The runner should say which, rather than leaving a reader to assume.

### 6.7 Where this lands architecturally

Nothing in §6.1–§6.6 requires a new mechanism beyond §4 and §5. It
requires the measure contract to carry **denominator, exclusions with
reasons, completeness, definition version and period** — which is why
those are in the contract rather than left to each measure — and it
requires the chart to be coded, which it already is.

It does add one thing §5 did not anticipate, and §11 Q8 asks about it:
**risk adjustment is part of a measure definition, not a separate
report.** An unadjusted outcome and an adjusted one are different
measures with different denominators, and treating adjustment as a
presentation choice is how a dashboard ends up comparing centres on
something it never measured.

## 7. Where core has to grow<a name="7-core"></a>

Three additions, and the argument for each being core rather than module.

### 6.1 A site dimension

Comparing one service against another is the entire point of a
benchmark, and hdh has `Provider` with nothing above it. A `Site` (or
organisation) entity is not specialty-specific — every module wants it,
the agent's population questions want it, and #88's dashboards want it.
**Core.**

The migration question is what an existing single-site dataset becomes;
§11 Q1 asks it.

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

## 8. Agent-first: dashboards without a dashboard<a name="8-agent"></a>

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

## 9. Ownership: module or core<a name="9-ownership"></a>

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

## 10. Milestones<a name="10-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **M1** | Core: the `Site` dimension, and the finder discovery hook. No specialty code yet. | a module can register a care-gap finder without `caregaps` knowing it exists |
| **M2** | Core: the measure contract — definitions as data, results carrying their evidence, the three shapes — and a runner. Validated at load. | a measure definition naming a missing field fails loudly |
| **M3** | The specialty module: pathway schema, closed event vocabulary, `AssessmentScore`, a fabricated fixture, and a condition pack generating synthetic pathway encounters | `GENERATOR_MODULES` gets its first real user; the chart still exports and reads correctly without the module loaded |
| **M4** | Transcribed measure definitions from a published set, with eligibility rules and their exclusions | a rate, an interval and a distribution all compute — and an uncomputable denominator reports itself rather than returning zero |
| **M5** | The agent surface: measure selection, results as structured evidence, and the same computation via `hdh measures run` | a benchmark question answered in conversation, and the same number obtained without the agent |

Each milestone is human-tested before the next begins.

## 11. Open questions<a name="11-questions"></a>

**Q1 — Does the `Site` dimension go in core, and what happens to existing
data?** The argument for core is in §7.1. If yes: does an existing
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

**Q8 — Is risk adjustment part of a measure definition, or a separate
report?** §6.7 argues it is part of the definition: an unadjusted outcome
and an adjusted one have different denominators and answer different
questions, so treating adjustment as a presentation choice is how a
dashboard ends up comparing services on something it never measured. The
cost is that a definition then carries a model — which variables, fitted
how, on which reference population — and that is a heavier thing to
transcribe than a numerator. Does a first version report **unadjusted
only, labelled as such**, and refuse the cross-service comparison
entirely until adjustment exists?
