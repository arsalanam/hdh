# Comprehension Extraction Schema — Drill-Down Design (Draft)

**Parent:** [notes-comprehension-service.md](notes-comprehension-service.md) §14 Q1 ·
**Status:** REVIEWED — decisions inline (§11) · **Date:** 2026-08-15

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, review |
| | | |

This document answers the master design's first drill-down question:
**what exactly is a mention?** It fixes the contract between pipeline
stages 1–2 (segment, extract) and everything downstream — span
granularity, composite mentions, list handling, and section-scoped
assertion defaults. The master doc's house rule governs every choice
here: *the LLM classifies, deterministic code decides* — and its
corollary for this stage: **extraction finds and types; it never codes
and never asserts.**

---

## Contents

1. [The Mention contract](#1-mention)
2. [Span rules](#2-spans)
3. [Attributes: sub-structure inside a mention](#3-attributes)
4. [Relations: composite clinical statements](#4-relations)
5. [Sections and assertion defaults](#5-sections)
6. [List handling and shared triggers](#6-lists)
7. [The closed LLM output schema and its validator](#7-schema)
8. [Storage shape (registry entities)](#8-storage)
9. [Worked examples](#9-examples)
10. [End-to-end: from note to actionable record](#10-end-to-end)
11. [Milestones](#11-milestones)
12. [Baseline](#12-baseline-milestone-b-2026-08-15)
13. [Open questions (answered)](#11-open-questions)
14. [Comprehensive testing plan (milestone E)](#14-testing)

---

## 1. The Mention contract<a name="1-mention"></a>

A **mention** is one span of note text that names one clinical entity of
one type. Frozen dataclass (the house contract style):

```python
@dataclass(frozen=True)
class Mention:
    id: int                     # 0-based, unique within the note
    mention_type: MentionType   # PROBLEM | MEDICATION | LAB_VITAL | PROCEDURE | ALLERGY
    span: Span                  # (start, end) into the ORIGINAL note text
    text: str                   # MUST equal note[start:end] — validator-enforced
    section_id: int             # which segment (§5) contains it
    attributes: tuple[MentionAttribute, ...]   # §3 — dose, value, laterality…
```

What a mention deliberately does **not** carry at extraction time:

| Absent field | Owner | Why |
|---|---|---|
| `code` / `ontology` | stage 3 (normalize) | extraction never codes — the funnel does, deterministically |
| `assertion` | stage 4 (contextualize) | rules first, LLM adjudicates disagreements; the section supplies the default (§5) |
| `confidence` | stages 3–5 | confidence is about linking and context, not about finding |

One entity, one mention. "Hypertension" appearing in both the history
line and the assessment is **two mentions** (different spans, different
sections, potentially different assertions) that stage 6 may reconcile —
extraction never deduplicates, because deduplication is a clinical
judgment (master doc §14 Q4).

## 2. Span rules<a name="2-spans"></a>

1. **Verbatim invariant** (the safety property): `text == note[start:end]`,
   byte-for-byte. A mention failing this is rejected and the extraction
   retried with feedback — no fuzzy repair, ever.
2. **Minimal head phrase**: the span covers the tokens that *name* the
   concept, not the sentence around it. `"History of: Essential
   hypertension, Type 2 diabetes"` yields spans over `Essential
   hypertension` and `Type 2 diabetes` — not the header, not the commas.
3. **Modifiers join the span only when they change the concept**:
   `Chronic kidney disease, stage 4` is one problem span (stage changes
   the code); `severe headache` is a `headache` span with a `severity`
   attribute (severity modifies, it does not rename). The rule of thumb
   given to the extractor: *would a clinician say the modifier is part of
   the disease name?*
4. **Single contiguous span in v1.** Discontinuous mentions ("pain …
   radiating to the left arm") take the enclosing narrow span of the head
   ("pain") plus attributes for what v1 can capture; true multi-span
   mentions are deferred (§11 Q1).
5. **Nesting is legal, overlap of same-type heads is not**: `BP 142/88`
   (LAB_VITAL) may sit inside a sentence that also contains `lisinopril`
   (MEDICATION); two PROBLEM mentions may not claim overlapping head
   tokens. *Live-testing admission:* a same-type span fully **contained**
   in another collapses deterministically into the larger mention
   (attributes merged, relations re-pointed) rather than failing — models
   reliably re-emit a diagnosis as its own indication ("Lisinopril …for
   hypertension" next to "essential hypertension"), and that is noise,
   not an error worth a retry. Partial overlaps remain hard rejections.

## 3. Attributes: sub-structure inside a mention<a name="3-attributes"></a>

Composite surface forms carry their parts as **typed attribute
sub-spans**, each with the same verbatim invariant:

```python
@dataclass(frozen=True)
class MentionAttribute:
    kind: AttributeKind
    span: Span
    text: str      # == note[start:end]
```

| `AttributeKind` | Applies to | Example text |
|---|---|---|
| `DOSE` | MEDICATION | `500mg` |
| `FREQUENCY` | MEDICATION | `BID` |
| `DURATION` | MEDICATION | `x10 days` |
| `ROUTE` | MEDICATION | `PO` |
| `VALUE` | LAB_VITAL | `142/88`, `9.2` |
| `UNIT` | LAB_VITAL | `mmHg`, `%` |
| `INTERPRETATION` | LAB_VITAL | `(high)` |
| `LATERALITY` | PROBLEM, PROCEDURE | `left` |
| `BODY_SITE` | PROBLEM, PROCEDURE | `medial malleolus` |
| `SEVERITY` | PROBLEM, ALLERGY | `severe`, `moderate` |
| `STAGE` | PROBLEM | `stage 4` |
| `STATUS_WORD` | MEDICATION, PROCEDURE | `Start`, `Continue`, `performed` |
| `REACTION` | ALLERGY | `rash`, `anaphylaxis` |
| `CONTROL` | PROBLEM | `well controlled`, `worsening` — maps onto the chart's `Condition.controlled` in the applier *(added during milestone-A live testing: the model repeatedly needed it on fluent notes, and the consumer already existed)* |

Attribute spans usually nest inside or sit adjacent to their mention's
span; the validator requires only that they not cross section
boundaries. This table is **closed**: an extractor wanting a new kind is
a schema change, not a free string — RxNorm's dose-carrying normalize()
(master §11) and LOINC's value+unit pairing (master §12) consume exactly
these kinds, which is why they are fixed here.

## 4. Relations: composite clinical statements<a name="4-relations"></a>

The master doc's example — *"BP 142/88 on lisinopril"* — is **three
facts**: a vital with a value, a medication, and an implied treatment
link. The first two are mentions; the third is a relation:

```python
@dataclass(frozen=True)
class MentionRelation:
    kind: RelationKind      # v1: TREATS only
    source_id: int          # the medication / procedure mention
    target_id: int          # the problem / lab_vital mention
    inferred: bool          # False only when the text states it ("for HTN")
```

**v1 ships one relation kind.** `TREATS` (medication/procedure →
problem) is the one the care-plan endgame consumes (concern ↔
intervention) and the one our note corpus can ground. `inferred=True`
relations are hints for downstream stages, never facts: the validator
accepts them without span evidence, and stage 6 decides whether the
chart supports them.

### 4.1 The full relation taxonomy, and the admission criteria

So kinds are chosen, not accreted, the complete clinical map (i2b2/VA
2010 relation challenge · n2c2 2018 medication/ADE task · FHIR landing
fields). Note first the big simplification: everything the literature
treats as med-attribute or anatomical relations (dose, route, body
site…) is an **attribute** here (§3), not a relation.

| Kind (lineage) | Direction | FHIR landing | Verdict |
|---|---|---|---|
| `TREATS` (TrAP) | med/proc → problem | `MedicationRequest.reasonReference` | **v1** — consumer: care-plan |
| `MEASURES` (TeCP) | test → problem | `ServiceRequest.reasonReference` | **reserved v1.5** — consumer: care-gaps monitoring |
| `REVEALS` (TeRP) | test → problem | `Condition.evidence.detail` | **reserved v1.5** — consumer: validator grounding |
| `CAUSED_BY` / ADE (TrCP) | problem → med | `AdverseEvent` | deferred — trigger: adverse-event modeling |
| `SECONDARY_TO` (PIP) | problem → problem | extension (SNOMED "due to") | deferred — trigger: a reader; ground truth is free (the generator's ComorbidityLink already encodes it) |
| `IMPROVES` / `WORSENS` (TrIP/TrWP) | med → problem | none clean | deferred — trigger: treatment-response modeling |
| `NOT_GIVEN_BECAUSE` (TrNAP) | med → problem | `DetectedIssue` | deferred — trigger: contraindication modeling |
| temporal (i2b2 2012) | event ↔ time | — | out of scope — coarse temporality is ASSERTION's job |

**Admission criteria** — a kind enters the emitted schema only when all
three hold: (1) a consumer module reads it today, (2) a FHIR landing
field exists for it, (3) the note corpus expresses it so §8 evaluation
can measure it. `MEASURES`/`REVEALS` sit in the enum as *reserved*
(schema-known, extractor-silent) because they pass (2) and (3) now and
(1) the moment care-gaps consumes comprehension.

## 5. Sections and assertion defaults<a name="5-sections"></a>

Stage 1 (deterministic — regex over the pack's templates, never an LLM)
produces the segments extraction works within:

```python
@dataclass(frozen=True)
class Section:
    id: int
    kind: SectionKind
    span: Span                  # the whole segment, header included
    default_assertion: Assertion
```

For the `family_medicine` pack, matched against the real `render_soap`
output (and tolerant of LLM-polished variants):

| `SectionKind` | Matches | Default assertion |
|---|---|---|
| `HEADER` | `SOAP NOTE — …` line | — (no mentions extracted) |
| `SUBJECTIVE` | `S:` | `present` |
| `SUBJECTIVE_HISTORY` | `History of: …` sentence within S | `historical` |
| `SUBJECTIVE_FAMILY` | `Family history: …` sentence within S | `family_history` |
| `SUBJECTIVE_ALLERGY` | `Known allergies: …` sentence within S | `present` — mentions here are `ALLERGY` type (§11 Q3, decided) |
| `OBJECTIVE` | `O:` | `present` |
| `ASSESSMENT` | `A:` | `present` |
| `PLAN` | `P:` | `present` (orders: intended, not historical) |

The contract split: **the section supplies the default; stage 4 owns the
final assertion.** Extraction's only assertion-adjacent duty is
`section_id`. Stage 4 starts from the section default and overrides on
local triggers (NegEx-style cues: "no", "denies", "resolved", "possible"),
with the LLM adjudicating only where rules disagree — exactly the master
§3 stage-4 contract. A section the segmenter cannot classify becomes
`UNKNOWN` with default `present` and a low-confidence flag, never a
silent skip.

## 6. List handling and shared triggers<a name="6-lists"></a>

Generated and real notes are list-dense. Three rules:

1. **Each list item is its own mention.** `History of: Essential
   hypertension, Type 2 diabetes mellitus.` → two PROBLEM mentions.
   `A:` with `Influenza (J11.1); Acute URI (J06.9)` → two mentions (the
   parenthetical codes are just text — extraction does not read them, and
   the evaluator will happily catch a funnel that disagrees with them).
2. **Distributed triggers annotate every item.** In `No fever, chills,
   or night sweats`, stage 4 needs to know one "No" governs three
   mentions. Extraction records the trigger ONCE as a section-local
   `shared_trigger` span on the segment (not per mention); stage 4
   distributes it. Extraction still does not decide what the trigger
   *means*.
3. **Plan-line verbs are attributes, not new types.** `Start Lisinopril
   10mg QD.` / `Continue Metformin…` → MEDICATION mention with a
   `STATUS_WORD` attribute; stage 6 maps status words onto
   MedicationStatement semantics.

## 7. The closed LLM output schema and its validator<a name="7-schema"></a>

Stage 2 calls the LLM once per **whole note** (§11 Q4, decided: cross-
section context for relations outweighs cheaper per-section retries;
revisit only if the eval corpus disagrees), with a **closed JSON
schema** (structured output — the icd10cm `AxisExtraction` pattern at
larger scale). Implementation note: the model emits `text` +
`occurrence` (nth appearance); a deterministic locator derives the
offsets — models are unreliable at character arithmetic, and the
verbatim invariant must never depend on it. The validated form:

```json
{
  "mentions": [
    {
      "type": "MEDICATION",
      "start": 412, "end": 422, "text": "Lisinopril",
      "attributes": [
        {"kind": "DOSE", "start": 423, "end": 427, "text": "10mg"},
        {"kind": "STATUS_WORD", "start": 406, "end": 411, "text": "Start"}
      ]
    }
  ],
  "relations": [
    {"kind": "TREATS", "source": 0, "target": 2, "inferred": true}
  ]
}
```

The deterministic validator rejects (with a reason string that becomes
the retry feedback, max 3 tries — the pattern-compiler loop):

- any `text != note[start:end]` (mention or attribute);
- unknown enum values (types, attribute kinds, relation kinds);
- attribute kinds illegal for the mention type (a `DOSE` on a PROBLEM);
- relation ids that don't resolve, or a `TREATS` whose source/target
  types are wrong;
- same-type head-span **partial** overlaps (§2 rule 5 — fully contained
  same-type spans collapse into the larger mention instead);
- mentions outside the note, or outside their claimed section's span;
- reserved relation kinds (`MEASURES`/`REVEALS`) — schema-known, extractor-silent until their consumers land (§4.1).

What survives validation is the **only** thing stages 3–7 ever see.
Failure discipline: a note that cannot validate after retries fails
loudly (master §3) — nothing half-extracted is stored.

## 8. Storage shape (registry entities)<a name="8-storage"></a>

The comprehension module declares (schema registry, like every module):

| Entity | Columns (sketch) |
|---|---|
| `NoteRecord` | id, visit_note_id FK, pack, pipeline_version, created_at, status (complete / needs_review / failed) |
| `NoteMention` | id, record FK, mention_type, start, end, text, section_kind, assertion, concept_id FK→ontology_concepts (nullable until normalized), confidence, properties (attributes + relations as JSON) |

Two deliberate choices: attributes/relations live in `properties` JSON
(they are read whole-mention, never queried relationally — promote to
tables only when a consumer needs SQL over doses); and
`NoteMention.concept_id` is the **only** place comprehension touches the
shared ontology tables — through the FK, never through hierarchy columns
(master §5 rules apply; the quality gate already enforces them).

## 9. Worked examples<a name="9-examples"></a>

Against a real generated note (abridged; offsets illustrative):

```
S: 67-year-old female presents with: Palpitations / irregular heartbeat.
   Known allergies: Penicillin. History of: Essential hypertension,
   Chronic kidney disease, stage 3a. Family history: mother: type 2 diabetes.
O: Vitals: BP 142/88 mmHg, HR 112, ... Notable labs: INR 2.4 ratio (high).
A: Unspecified atrial fibrillation (I48.91)
P: Start Apixaban 5mg BID. Follow up in 90 days.
```

| Text | Extraction |
|---|---|
| `Palpitations / irregular heartbeat` | PROBLEM, section SUBJECTIVE (default `present`) |
| `Penicillin` | ALLERGY, section SUBJECTIVE_ALLERGY |
| `Essential hypertension` | PROBLEM, section SUBJECTIVE_HISTORY (default `historical`) |
| `Chronic kidney disease, stage 3a` | PROBLEM + `STAGE`=`stage 3a`, SUBJECTIVE_HISTORY |
| `type 2 diabetes` | PROBLEM, SUBJECTIVE_FAMILY (default `family_history`) |
| `BP` | LAB_VITAL + `VALUE`=`142/88`, `UNIT`=`mmHg` |
| `INR` | LAB_VITAL + `VALUE`=`2.4`, `UNIT`=`ratio`, `INTERPRETATION`=`(high)` |
| `Unspecified atrial fibrillation` | PROBLEM, ASSESSMENT (`(I48.91)` is untouched text) |
| `Apixaban` | MEDICATION + `DOSE`=`5mg`, `FREQUENCY`=`BID`, `STATUS_WORD`=`Start` |
| relation | `TREATS(Apixaban → atrial fibrillation, inferred=true)` |

The master doc's composite — *"BP 142/88 on lisinopril"* in a polished
variant — resolves as: LAB_VITAL(`BP`, VALUE, UNIT) + MEDICATION
(`lisinopril`) + `TREATS(lisinopril → BP-context, inferred=true)`; the
implied indication (hypertension) is **stage 6's** to resolve against
the chart, not extraction's to invent — there is no hypertension span in
that sentence, and no span means no mention.

## 10. End-to-end: from note to actionable record<a name="10-end-to-end"></a>

The ultimate goal, stated by review: comprehension turns a free-text
note into (a) a **structured encounter record** (FHIR), (b) an
**actionable chart update**, and (c) a **regenerable SOAP note** — the
same encounter, equally usable by humans, AI agents, and non-AI systems.
The §9 note walked all the way through:

### 10.1 After stages 3–5 (normalize, contextualize, disambiguate)

| Mention | Home ontology → code | Assertion (stage 4) |
|---|---|---|
| Palpitations / irregular heartbeat | SNOMED 80313002 *Palpitations* | present |
| Penicillin | ALLERGY mention → SNOMED substance | allergy |
| Essential hypertension | SNOMED 59621000 · billing view I10 via `maps_to` | historical, active |
| CKD, stage 3a | SNOMED 700378005 · N18.31 | historical, active |
| type 2 diabetes | SNOMED 44054006 | family-history (mother) |
| BP 142/88 mmHg | LOINC 55284-4 (components 8480-6 / 8462-4) | present |
| HR 112 | LOINC 8867-4 | present |
| INR 2.4 (high) | LOINC 6301-6, interpretation H | present |
| Unspecified atrial fibrillation | SNOMED 49436004 · I48.91 (note's own code cross-checks the funnel) | present — encounter diagnosis |
| Apixaban 5mg BID | RxNorm (illustrative RxCUI 1364445) | ordered (STATUS_WORD `Start`) |
| TREATS(Apixaban → AFib) | — | resolved against the assessment (inferred → grounded) |
| Follow up in 90 days | SNOMED 185389009 *Follow-up visit* | ordered |

### 10.2 The FHIR message (stage 6): a transaction Bundle

One `Bundle(type=transaction)` — the actionable form. Reconciliation
(master §14 Q4) decides each entry's verb:

| Entry | Chart reconciliation | Action |
|---|---|---|
| `Encounter` (AMB, reasonCode 80313002) | new visit | POST |
| `Composition` (the structured note; S/O/A/P sections referencing every resource below; `NoteMention` rows keep span provenance) | new | POST |
| `Condition` — AFib 49436004/I48.91, encounter-diagnosis, onset = visit date | **not on chart → chart update** | POST (problem list gains AFib) |
| `Condition` — HTN, CKD 3a | already on chart, note agrees | reference existing (no duplicate) |
| `AllergyIntolerance` — Penicillin | already on chart | reference existing |
| `FamilyMemberHistory` — mother, T2DM | already on chart | reference existing |
| `Observation` ×3 — BP (component form, exactly our emitter shape), HR, INR (interpretation H) | new measurements | POST |
| `MedicationRequest` — apixaban, `status=active, intent=order`, dosage 5mg BID, `reasonReference` → the AFib Condition (the TREATS relation, now grounded) | new order → medication list update | POST |
| `ServiceRequest` — 185389009 follow-up, `occurrenceDateTime` = visit + 90d | new order | POST |

Had the note said *"CKD, stage 3b"* against a chart that says 3a, that
Condition would go to the **review queue** (master §3 stage 7), not the
bundle — disagreement is a checkpoint, never a silent overwrite.

Every POSTed resource carries a provenance extension
`{record_id, mention_id}` back to its `NoteMention` (and thus its span);
the validator's grounding rule holds end to end: **no span, no mention,
no resource.**

### 10.3 Applying the update: an internal applier, not a FHIR server

Review surfaced the seam: the demo FHIR API is **read-only by design**
(hdh is its own system of record; FHIR is a view), so the transaction
Bundle cannot literally be POSTed anywhere — and must not need to be.
The Bundle is the **interchange artifact**; the update mechanism is an
internal, transactional **chart applier** in the comprehension module:
structured record in → ORM writes out, review queue as the gate, all in
one session transaction (nothing half-applied).

The model already holds almost everything the applier needs:

| Bundle entry | hdh structure | Status |
|---|---|---|
| Encounter | `Visit` | exists |
| Observation (vitals / labs) | `Vital` / `LabResult` | exists |
| Condition | `Condition` (unified problem list) | exists |
| MedicationRequest | `Prescription` + `MedicationStatement` | exists |
| AllergyIntolerance / FamilyMemberHistory | `Allergy` / `FamilyHistory` | exists |
| ServiceRequest (follow-up) | `Visit.follow_up_days` | exists (simple case) |
| ServiceRequest (referrals, standing orders) | — | **gap — deliberately deferred**: orders/interventions belong to the care-plan module's design, which can add an Order entity via the schema registry with zero core edits |
| Composition + spans | `VisitNote` + `NoteRecord`/`NoteMention` (§8) | this design |

**Decided (§11 Q5): FHIR writes are a documented non-goal.** The FHIR
API stays read-only; comprehension updates hdh directly through the
internal applier. The Bundle exists for export and interop
demonstration only.

### 10.4 The SOAP round-trip (stage 6, presentation path)

After the applier commits, the round-trip needs NO new renderer: the
applied `Visit` row renders through the existing `visit_to_soap()` —
proving the record is complete enough that the text form is now merely a
VIEW of the structure:

```
SOAP NOTE — 2026-08-15  (Dr. Sarah Mitchell, MD)
S: 67-year-old female presents with: Palpitations / irregular heartbeat.
   Known allergies: Penicillin. History of: Essential hypertension,
   Chronic kidney disease, stage 3a. Family history: mother: type 2 diabetes.
O: Vitals: BP 142/88 mmHg, HR 112. Notable labs: INR 2.4 ratio (high).
A: Unspecified atrial fibrillation (I48.91)
P: Start Apixaban 5mg BID. Follow up in 90 days.
```

The loop this closes: generator → note → comprehension → structured
record → note. On synthetic data both ends are known, which is the §8
evaluation strategy — any drift between the regenerated note and the
ground truth localizes the defect to a pipeline stage.

## 11. Milestones<a name="11-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **A** | contracts (five mention types, closed attribute/relation enums), deterministic segmenter (family-medicine pack), whole-note extraction (stub + LLM extractors behind one protocol), the validator with retry feedback, `NoteRecord`/`NoteMention` registry entities, `hdh comprehend --file` printing the validated extraction | stages 1–2 end-to-end, offline-testable against generated notes with zero LLM cost in CI |
| **B** | normalize (SNOMED funnel for PROBLEM/PROCEDURE/ALLERGY; LabSpec-derived LOINC map for LAB_VITAL and catalog drug names for MEDICATION as documented placeholders until the phase-3 modules land), contextualize (section defaults + NegEx-lite rules, LLM adjudicates disagreements), disambiguate (ancestor-set context), stored records with codes/assertions/confidence | stages 3–5; mention F1 + linking accuracy measured against generator ground truth (master §8) |
| **C** | assemble + validate (stages 6–7): typed FHIR Composition/Bundle export reusing the R4B emitters, the internal chart applier with reconciliation verdicts and the review queue, SOAP round-trip via `visit_to_soap` | the closed loop: note → structure → chart update → note |
| **D** | `comprehend_note` agent tool (published API, catalog-gated) + review-loop CLI basics; `apply_note` (free-text charting from chat: provider attribution, same-date visit reconciliation) | the agent as prime consumer (master §13 phase 6) — and the provider chart-maintenance flow live |
| **E** | the comprehensive testing plan (§14): replay corpus, property-based span tests, applier verdict matrix, scripted agent E2E scenarios, scorer fixes, eval rerun against the §12 baseline | comprehension trusted enough to regression-proof — every future prompt/funnel/model change measured, every live failure pinned |

## 12. Baseline (milestone B, 2026-08-15)

First measured numbers — 10 stored notes, LLM extraction, full SNOMED
catalog: **mention recall 61.0% · precision 39.8% · F1 48.2% · linking
59.4% · assertion 72.7%.** Read with three caveats that ARE the next
tuning targets: (1) precision is structurally understated — the
ground-truth builder omits vitals, so ~7 correct extractions per note
count as unmatched (scorer fix queued); (2) recall misses cluster on
"History of:" chronic problems — extractor variance on list-dense
history lines, a prompt target; (3) linking measures funnel-top-choice
against `hdh ontology tag`'s mapping — consistency between the two
coding paths matters as much as raw accuracy. Every future change —
prompt, funnel, SapBERT — is measured against this table.

## 13. Open questions<a name="11-open-questions"></a>

1. **Discontinuous spans** (deferred from §2): v2 as
   `spans: tuple[Span, ...]`? Wait for eval data showing v1's enclosing
   spans actually mislink. deffered as v2
2. ~~**Relation kinds beyond TREATS**~~ **Resolved by §4.1**: the full
   taxonomy is mapped, admission criteria fixed; minimum set = `TREATS`
   (v1) + `MEASURES`/`REVEALS` (reserved v1.5); five kinds deferred with
   named triggers.
  
3. **Allergy modeling**: allergen-as-PROBLEM (proposed, keeps four types)
   vs a fifth `ALLERGY` mention type — decide when AllergyIntolerance
   export lands in stage 6.  fifth Allergey mention type
4. **Section batches vs whole-note extraction**: per-section calls are
   cheaper to validate and retry but lose cross-section context for
   relations; the eval corpus (master §8) should measure both before the
   choice hardens. lets try with whole first
5. **Narrow `POST /Bundle`** (from §10.3): worth shipping as an interop
   demonstration once the internal applier exists, or does it stay a
   documented non-goal? If shipped: our own transaction shape only,
   delegating to the applier — never a general FHIR write server.
    yes posts are just documented non goals and we will directly update HDH
## 14. Comprehensive testing plan (milestone E)<a name="14-testing"></a>

Five layers, cheapest first; a change must pass every layer below it
before the expensive ones run. The standing gates (`pytest`, `just qa`,
`scripts/test_pg.py`, CI on SQLite + Postgres) wrap all of it.

### 14.1 Deterministic unit layer (CI, zero LLM)

What exists: 40+ offline tests over contracts, segmenter, locator,
validator rejection classes, containment collapse, normalizer routing,
NegEx-lite triggers, applier verdicts, FHIR assembly, agent tools.
What E adds:

- **Property-based span tests** (hypothesis): for arbitrary generated
  notes and arbitrary in-note substrings, the locator + validator must
  preserve the verbatim invariant `text == note[start:end]` and never
  produce out-of-section spans — fuzz what live testing found by luck.
- **Applier verdict matrix**: one parameterized table covering every
  (entity kind × chart state × assertion) cell — new/confirmed/review/
  skipped each provably reachable and exclusive; dry-run leaves zero rows
  for every cell.
- **Locator torture cases**: repeated tokens, punctuation-adjacent
  matches, occurrence counts past the last match, case collisions.

### 14.2 Replay corpus (offline regression, golden files)

Every live LLM failure becomes a fixture: the raw extractor JSON that
broke us, stored under `tests/fixtures/comprehension/replays/`, replayed
through the real validator + pipeline via `stub_extractor`. Seeds from
this arc: nested same-type mention ("hypertension" in "essential
hypertension"), units folded into values ("98.5F"), CONTROL demanded on
problems, duplicate relations, relation-to-rejected-mention. The rule:
**no live failure is fixed without its replay landing in the corpus.**

### 14.3 Scorer fixes + eval rerun (LLM, on demand)

The §12 caveats are work items, not footnotes:

1. ground-truth builder emits vitals (kills the structural precision
   understatement);
2. history-line recall measured as its own metric slice so prompt tuning
   has a target;
3. rerun `hdh comprehend --eval` at N=25 and record the new table beside
   the §12 baseline — every future prompt/funnel/SapBERT change reruns
   the same N and may not regress any column without a written reason.

### 14.4 Postgres parity

The funnel is dialect-sensitive (FTS ranking, exact-term boost live only
on PG). E pins PG-specific funnel tests in `scripts/test_pg.py`: exact
term beats FTS, prefix ≥ 0.85, ranking on raw scores — the fatigue→
"Exercise induced muscle fatigue" bug class can never return silently.

### 14.5 Scripted agent E2E scenarios

The chat flows proved live this week, frozen as scripted tests (stub
extractor, scripted tool calls — no LLM, no chat):

- chart a note for a new date → visit created, provider attributed;
- addendum on the same date → reconciles into the existing visit, never
  duplicates;
- review item → NOT written, surfaced with instructions;
- guardrail probe → a write attempted through `query_database` is
  refused and the session survives (tool_guard rollback);
- unknown MRN / unknown provider / "yesterday" date handling.

### 14.6 Explicit non-goals of E

Deferred with triggers, consistent with the master design: CI-run LLM
evals (cost — stays on-demand), load/perf testing (no concurrency story
yet), chart amend/delete + audit-log testing (its own arc, issue filed
at ship time), per-section extraction comparison (§13 Q4 — needs the
eval corpus from 14.3 first).
