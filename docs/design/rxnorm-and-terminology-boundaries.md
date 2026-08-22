# RxNorm, and Where Terminology Work Belongs (Draft)

**Where it lives:** `hdh.modules.rxnorm` (the drug graph) ·
`hdh.core.termsearch` (the shared funnel) ·
**Follows:** [service-requests-and-interchange.md](service-requests-and-interchange.md) §7–§9 Q7 ·
**Status:** DRAFT · **Date:** 2026-08-21

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, review; the boundary thesis — an ontology module stores and maintains, search belongs to its callers |
| | | |

RxNorm is the third vocabulary module, and building the second one proved
we have the boundary wrong. This document is therefore two things: the
design for medication coding, and the interface correction that has to
land with it — because a third copy of the same mistake is much harder to
undo than a second.

**The thesis, in one line:** an ontology module should be responsible for
**storing a vocabulary so it can be retrieved and maintained**, and search
strategy belongs to the layers that know what the search is *for*.

---

## Contents

1. [The evidence: what building LOINC exposed](#1-evidence)
2. [Reviewing the SNOMED module: what each surface is really for](#2-review)
3. [The corrected boundary](#3-boundary)
4. [Contracts: the RxNorm graph](#4-contracts)
5. [Why drug matching is compositional, not lexical](#5-composition)
6. [What changes for `Prescription` and the formulary](#6-existing)
7. [The agent surface](#7-agent)
8. [Applying the correction to LOINC and SNOMED](#8-retrofit)
9. [Milestones](#9-milestones)
10. [Test scenarios: notes that are actually hard](#10-scenarios)
11. [Open questions — answered](#11-questions)

---

## 1. The evidence: what building LOINC exposed<a name="1-evidence"></a>

Milestone D's LOINC module was written the way the SNOMED module was
written, and the copying is not a figure of speech:

| | `snomed/ontology.py` | `loinc/ontology.py` |
|---|---|---|
| `PARTIAL_COVERAGE_CEILING = 0.55` | line 37 | line 49 |
| exact-term query, then FTS, then trigram | `_search_terms` | `_search_postgres` |
| `ts_rank(..., 1)` scoring, `0.5 + 0.4 * r/top` | yes | yes |
| per-word coverage rule | `_covers_every_word` | **imported from SNOMED** |

That last row is the tell. `loinc/ontology.py:230` reads:

```python
from hdh.modules.snomed.ontology import _covers_every_word
```

A module reaching into another module's **private function**, against the
rule stated in `hdh/modules/__init__.py`:

> Modules may depend on the core and on optional third-party extras, but
> never on each other's internals.

It was the honest thing to write at the time — the alternative was a
third copy of a rule that issue #54 had just measured into existence —
and that is precisely the point. The rule was pulling toward a shared
home that does not exist yet.

Three further facts say the same thing:

- **The #54 lessons were never about SNOMED.** Exact-term-first, the
  per-word coverage cap, "a fuzzy match is a guess about the words it did
  not cover" — all of it is about lexical retrieval. It applied unchanged
  to LOINC. It will apply unchanged to RxNorm.
- **Two callers already search, and they are peers.**
  `comprehension/normalize.py` calls `normalize()`, and so does
  `snomed/agent_tools.py:53`. Search cannot move *into* comprehension
  without the agent module having to depend on comprehension, which is
  backwards.
- **The vocabulary-specific parts are small and real.** SNOMED's semantic
  tags, LOINC's specimen axis, RxNorm's term types. They are the only
  parts that genuinely differ, and they are configuration, not algorithm.

## 2. Reviewing the SNOMED module: what each surface is really for<a name="2-review"></a>

The module has four surfaces. Sorting them by *who needs them and why* is
what the boundary falls out of.

| Surface | What it does | Belongs to |
|---|---|---|
| `loader/` (RF2 parse, closure build, indexes) | turns a licensed release into rows | **the module** — a vocabulary's storage is its own business |
| `lookup` / `ancestors` / `descendants` / `subsumes` / `synonyms` | answers questions *about the graph* | **the module** — only it knows whether its hierarchy is a closure table or a path |
| `normalize()` rungs and scoring | free text → ranked candidates | **shared** — identical across vocabularies (§1) |
| `normalize()` tag/axis boosts | prefers a disorder over a body structure | **the module** — vocabulary-specific, but declarative |
| `agent_tools.py` | exposes the above to an LLM | **the module**, over the protocol |

The two rows in the middle are currently welded together inside one
method, which is why the whole thing had to be copied.

Worth recording explicitly, because it is the part that is *right*:
`ontology_closure` never leaves the SNOMED module, `path` never leaves
ICD-10-CM and LOINC, and the quality gate enforces it. The
**hierarchy-storage** boundary was drawn correctly from the start. It is
only the **search** boundary that was never drawn at all.

## 3. The corrected boundary<a name="3-boundary"></a>

```
   comprehension                     agent module
   (owns the NOTE)                   (owns the CONVERSATION)
        │                                   │
        │  mention + section + attributes   │  free-text question
        ▼                                   ▼
   ┌───────────────────────────────────────────────┐
   │  OntologyService.normalize()  (unchanged)     │
   └───────────────────────────────────────────────┘
        │                delegates
        ▼
   ┌───────────────────────────────────────────────┐
   │  hdh.core.termsearch      ONE funnel           │
   │  exact → abbreviation → FTS → trigram          │
   │  coverage rule · confidence ceilings           │
   └───────────────────────────────────────────────┘
        │            shaped by
        ▼
   ┌───────────────────────────────────────────────┐
   │  the module's SearchProfile (declarative)      │
   │  snomed: semantic tags, ABBR- aliases          │
   │  loinc:  specimen axis, class                  │
   │  rxnorm: term-type tiers (IN < SCD < SBD)      │
   └───────────────────────────────────────────────┘
```

Three rules follow, and they are the whole design:

1. **A vocabulary module owns storage, loading, the graph, and a
   `SearchProfile`.** It never implements a funnel.
2. **`hdh.core.termsearch` owns retrieval and scoring.** One place to fix
   a ranking bug, one place to add a rung.
3. **`OntologyService` keeps its current shape.** Consumers see no change
   — `normalize()` still returns ranked `Candidate`s — so this is an
   internal correction, not a protocol migration. That matters: the
   protocol is cheap to change at three implementers and expensive at
   five.

**Where semantic search lands, and why it is not here.** Issue #54's
residue needs *recall* — the right concept is not retrieved at all — and
a dense rung belongs in `termsearch` beside the lexical ones, benefiting
all three vocabularies at once. But **context-aware reranking belongs to
comprehension**, because only comprehension holds the sentence: `SOB` in
isolation carries no signal, and "patient reports SOB on exertion" does.
So the split is not "lexical here, semantic there" — it is *retrieval is
shared, and interpretation belongs to whoever owns the context*.

## 4. Contracts: the RxNorm graph<a name="4-contracts"></a>

RxNorm is not a hierarchy of drugs; it is a graph of **term types**
joined by typed relationships, and pretending otherwise is the usual way
to get it wrong.

```
   IN     Lisinopril                     ingredient
    │ ingredient_of
   SCDC   Lisinopril 10 MG               component (ingredient + strength)
    │ constitutes
   SCD    Lisinopril 10 MG Oral Tablet   the prescribable clinical drug
    │ tradename_of ▲
   SBD    Zestril 10 MG Oral Tablet      the branded equivalent
                   │ has_ingredient
   BN     Zestril                        brand name
```

Loaded from the standard release files:

| File | What we take |
|---|---|
| `RXNCONSO.RRF` | atoms: RXCUI, TTY, STR, SAB — the concepts and their names |
| `RXNREL.RRF` | typed edges: `has_ingredient`, `tradename_of`, `consists_of`, `has_dose_form` |
| `RXNSAT.RRF` | attributes: strength, dose form, `RXN_AVAILABLE_STRENGTH` |

Storage reuses the shared tables exactly as SNOMED and LOINC do —
`ontology_concepts` (`rxnorm:314076`), `ontology_terms`, `ontology_edges`
for the typed relationships. **No new tables.** The `edge_type` enum
already carries what a graph needs; the drug-specific relation names
travel in `properties`.

Distribution: RxNorm ships through UTS, and the SNOMED loader already
downloads from UTS with `UMLS_API_KEY`. That acquisition code is worth
sharing rather than copying — the same lesson as the funnel, caught
earlier this time (§9 M1).

## 5. Why drug matching is compositional, not lexical<a name="5-composition"></a>

This is the section that most changes what gets built.

A note says **"Start Lisinopril 10mg once daily"**. The prescribable
concept is called **"Lisinopril 10 MG Oral Tablet"**. Those two strings
are not close, and no amount of ranking makes them close: the note omits
the dose form entirely and writes the strength differently. Meanwhile
`"Lisinopril"` alone matches the *ingredient*, which is a different
RXCUI at a different level of the graph.

But comprehension already extracts what is missing, as typed attributes:

```
    mention   "Lisinopril"        MEDICATION
    attrs     DOSE   "10mg"
              ROUTE  "PO"
              FREQ   "once daily"
```

So the resolution is a **composition**, not a similarity score:

1. lexical search resolves the *ingredient* — one word, exact or near —
   which is the part lexical search is good at;
2. the graph walks `ingredient_of → SCDC` filtered by the extracted
   strength;
3. `constitutes → SCD` filtered by the dose form implied by the route;
4. and if any step is ambiguous, the request stays **coded at the level
   we are sure of** — the ingredient — rather than guessing a tablet.

**A branded order carries the branded code** (§11 Q5). When the note names
a brand, the walk ends at the SBD rather than the SCD, because what was
prescribed is what the chart should say — and the ingredient stays one
graph hop away for any analysis that wants it. Scenario A's "Junovia"
(§10) is this path with a misspelling in front of it.

That last point is the refuse-don't-guess contract in its RxNorm form. It
also answers a question that would otherwise be a coin toss: *which level
do we code at?* The answer is **the deepest level the evidence supports**,
recorded so a reader can see why.

Note what this means for the boundary: step 1 is `termsearch`'s job,
steps 2–3 are the RxNorm module's graph, and the attributes driving them
come from comprehension. Each layer does the part it is equipped for, and
no layer needs to know how another works.

## 6. What changes for `Prescription` and the formulary<a name="6-existing"></a>

**`Prescription` keeps its free-text fields.** They record what was
written, and a code is an annotation on that, never a replacement — the
same reasoning that kept order and dispense separate (§9 Q2 of the
service-requests design).

What it gains is the same pair `ServiceRequest` already has:

```python
    code_system: Mapped[str | None]   # "rxnorm"
    code: Mapped[str | None]          # RXCUI, at the level we are sure of
```

**`MedicationStatement` gains the same**, because the cross-visit
medication list is what a reconciliation actually reads.

**The formulary is where the interesting drift is.** `RxSpec.drug_name`
is free text authored by hand in `disease_engine.py` — "Lisinopril",
"Hydrochlorothiazide", "Amoxicillin-Clavulanate". Once RxNorm is loaded
those can carry an RXCUI, which turns the catalog from a list of strings
into a list of *drugs*, and makes generated prescriptions codeable
without any note comprehension at all. That is a genuine improvement and
also a scope risk, so §10 Q4 asks whether it is in or out.

## 7. The agent surface<a name="7-agent"></a>

`build_snomed_tools` gives an LLM four tools: normalize, lookup,
subsumes, descendants. RxNorm's are the same four plus the two the graph
makes possible — and the interface question the user raised is answered
by the boundary rather than by the tools:

- the agent talks to **`OntologyService`**, never to storage;
- the tools are thin — parse arguments, call the protocol, format a
  string — and any that grows a scoring rule has put logic in the wrong
  place;
- `rxnorm_ingredients_of(rxcui)` and `rxnorm_brands_of(rxcui)` are new,
  because they are graph questions rather than search questions, and they
  are exactly what a medication-reconciliation conversation needs.

The rule worth writing down: **an agent tool may not contain a decision
that a non-agent caller would also need.** If it does, that decision
belongs in the module and the tool should call it.

## 8. Applying the correction to LOINC and SNOMED<a name="8-retrofit"></a>

The retrofit is the point of doing this now, and it is deliberately
staged so the funnel is proven before the third vocabulary lands on it.

The measurement that makes it safe already exists: the **`fullload`
stress suite** (#53) with its `MAX_CONFIDENT_WRONG` ratchet. Extracting
the funnel must not move that number — and the suite runs against a real
loaded edition, so it will say so.

Retrofit order: SNOMED first (it has the measurement), LOINC second (it
has the fixture), and only then RxNorm — which is written against
`termsearch` from the start and never gets its own copy.

## 9. Milestones<a name="9-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **M1** | `hdh.core.termsearch`: the funnel, the coverage rule, the ceilings, a `SearchProfile` contract. SNOMED delegates to it. | the #53 ratchet does not move |
| **M2** | LOINC delegates too; a quality-gate check for module **privacy** — using another module's public API is fine (it is the agent's whole job), reaching past it is not | no module reaches into another's internals |
| **M3** | `hdh.modules.rxnorm`: loader (RRF), graph edges, `SearchProfile`, `OntologyService` #4, fabricated fixture | a drug vocabulary lands with no funnel of its own |
| **M4** | Compositional coding: ingredient → strength → form, at the deepest supported level and **branded when the note names a brand** (§11 Q3, Q5); `Prescription`/`MedicationStatement` gain code columns; `RxSpec` carries an RXCUI (§11 Q4); `hdh rxnorm code` | **Scenario A's medication rows** (§10): an ER dose form, a quantity-times-strength, a verbatim sig, and a misspelt brand |
| **M5** | Agent tools; the comprehensive test plan over the §10 scenarios | the interface holds under a conversation, and against notes we did not write |

Each milestone is human-tested before the next begins.

**Shared UTS acquisition moved from M1 to M3.** It was listed here on the
assumption that RxNorm downloads the way SNOMED does, but there is exactly
one implementation to generalise from — and generalising from one is the
mistake this whole document is correcting. The funnel was worth extracting
because two copies existed and the differences between them were visible.
The downloader gets the same treatment when RxNorm gives it a second case.

## 10. Test scenarios: notes that are actually hard<a name="10-scenarios"></a>

Every note in the test corpus so far was written by us, and it shows: they
have SOAP headers, one drug per sentence, and a strength written the way
the catalog writes it. Real notes are compressed, misspelt, and full of
things that are mentioned without being ordered.

These scenarios are the corpus for the comprehensive plan. **RxNorm's own
milestones test the medication rows only** — the rest are recorded here so
they are not re-invented, and so the modules that own them can be measured
against the same notes rather than against convenient ones.

### Scenario A — the diabetes follow-up

> patient with h/o type 2 diabetes and well treated hypertension came with
> higher than 7 Hba1c … i continued Metformin ER 2 x 500mg with evening
> meal and added Junovia 25 mg OD and asked for repeat HbA1c after 90 days
> .. eyesight and foot exam was normal .. refill and new drug order placed

One sentence per line of a real chart, and eleven distinct problems:

| Fragment | What it is | What must happen | Why it is hard |
|---|---|---|---|
| `h/o type 2 diabetes` | problem | charted, chronic, ACTIVE | "h/o" means *established*, not *resolved* — the opposite reading is a silent inversion |
| `well treated hypertension` | problem + control | charted with `controlled = True` | the qualifier is the clinical fact; dropping it loses why no change was made |
| `higher than 7 Hba1c` | lab RESULT | `value 7`, `comparator ">"` | a result stated in prose, not a vitals line — and the comparator column exists precisely for this |
| `continued` | status word | `is_new = False` | continuation vs new is what makes the refill row correct |
| `Metformin ER` | medication, generic | RxCUI at **extended-release** dose form | "ER" changes the product: ER 500 MG ≠ 500 MG. Drop it and the code is a different drug |
| `2 x 500mg` | quantity × strength | strength 500 mg, quantity 2 | the note's arithmetic is not the label's — 1000 mg is the dose, 500 mg is the product |
| `with evening meal` | frequency/timing | `sig` verbatim | not a frequency code; the sig is what a pharmacy reads |
| `Junovia 25 mg OD` | medication, **brand, misspelt** | fuzzy → Januvia → **branded** RxCUI (§11 Q5) | one edit from a brand name, and brands are a different TTY at a different level |
| `repeat HbA1c after 90 days` | LAB order | request, `occurrence_date = visit + 90d` | a request and a result for the same test in one note |
| `eyesight and foot exam was normal` | two procedures | charted as performed, normal | diabetic eye and foot exams are care-gap items — losing them costs a gap |
| *(no blood pressure anywhere)* | **absence** | **no vitals row** | hypertension is discussed at length; a system that infers a BP has invented a clinical fact |

**What this note already breaks.** It has no SOAP headers — it is prose,
which is how most real notes look — so `segment()` returns a single
`UNKNOWN` section and **no PLAN**. Milestone B's fifth pass keys on the
plan section to tell a request from a result, so against this note it
creates **zero orders**: both drugs, the HbA1c request and the refill are
silently dropped. Verified, not predicted.

That is the right thing for the fifth pass to do given what it can see,
and the wrong outcome. It means unstructured notes need an ordering signal
that is not the section — the status words are already extracted
(`continued`, `added`, `asked for`, `placed`), and they say what the
section cannot. Recorded here rather than fixed in passing, because it
belongs to comprehension and not to RxNorm.

**The medication rows are RxNorm's milestone-4 acceptance test.** Rows 5–8
are the compositional path of §5 end to end: an ER dose form, a
quantity-times-strength, a verbatim sig, and a misspelt brand that has to
resolve through the graph rather than by string similarity.

### Scenario B — the switch, with a reason

> stopped lisinopril due to persistent dry cough, started losartan 50 mg
> daily instead

- a medication **stopped**, with `stop_reason` — the OMOP field §3 of the
  service-requests design adopted, exercised for the first time by a note
- a medication started *because* the other stopped: two requests whose
  relationship is the point
- "instead" is the only thing linking them, and losing it makes the chart
  read as a patient on both an ACE inhibitor and an ARB — a combination
  that is actively contraindicated

### Scenario C — the combination product

> continue Janumet 50-1000 BID

- one brand naming **two ingredients** (sitagliptin + metformin), which in
  RxNorm is a multiple-ingredient concept, not two drugs
- `50-1000` is a paired strength, and splitting it wrongly produces two
  plausible and wrong codes
- the reconciliation question this exists to test: a patient on Janumet is
  already on metformin, so charting Scenario A's metformin *and* this
  would double an ingredient without ever naming it twice

### How these get used

1. Each scenario becomes a **replay fixture** — a note plus the record of
   what should come out — so a new failure is a new file rather than new
   code, which is the pattern the comprehension corpus already uses.
2. The medication rows are asserted by RxNorm's milestones. The rest are
   asserted by whichever module owns them, against **the same note**.
3. A scenario is never "passed" by loosening it. Rows that cannot be
   satisfied yet are recorded as expected failures with the reason, the
   way the #53 frontier list is — so the corpus says what is not working
   instead of quietly not asking.

## 11. Open questions — answered<a name="11-questions"></a>

All seven answered 2026-08-21. Six confirmed the proposal; **Q5 changed
it** — a branded order carries the branded RXCUI rather than the clinical
drug with the brand filed in `detail`, because what was prescribed is what
the chart should say (§5, §9 M4). Q7's answer became §10.


1. **Is the retrofit worth doing before RxNorm?** Proposal: yes — M1 and
   M2 first, so RxNorm never grows a funnel to remove later. The cost is
   two milestones before any new capability lands. The alternative is to
   build RxNorm on the current shape and retrofit all three afterwards,
   which is cheaper now and more expensive at every later step. Retrofit
   first, or capability first? Yes

2. **Where should `termsearch` live — `hdh.core` or its own module?**
   Proposal: `hdh.core`, because three modules depend on it and core is
   what modules are allowed to depend on. Against: it is a retrieval
   strategy rather than a chart concept, and core has so far meant "the
   chart". Core, or `hdh.modules.termsearch` with the ontology modules
   depending on it explicitly? Core

3. **Which RxCUI level do we store when the evidence is partial?**
   Proposal: the deepest level supported — SCD when strength and form are
   both known, SCDC with strength alone, IN when only the drug is named —
   with the level recorded so a reader can see what was inferred.
   Alternative: always the ingredient, and treat strength/form as
   attributes of the request rather than part of the code. Deepest, or
   always-ingredient? Deepest possible

4. **Should the condition catalog's `RxSpec` carry an RXCUI?** It would
   make generated prescriptions codeable with no comprehension involved,
   and turn the formulary from strings into drugs. It also means the
   catalog can no longer be authored without a licensed release to check
   against. In scope for M4, or its own arc? M4

5. **Brands.** Proposal: load SBD/BN and resolve *to* them when a note
   names a brand ("Zestril"), but code the request at the clinical-drug
   level with the brand recorded in `detail`. Or should a branded order
   carry the branded RXCUI, since that is what was actually prescribed?
    Branded RXCUI

6. **How far does the shared funnel go?** Proposal: `termsearch` owns
   every rung including a future dense one, and comprehension owns
   context-aware reranking because only it holds the sentence (§3). Is
   that the right seam, or should reranking also be shared so the agent
   gets it too? Yes agree with proposal

7. **What does the comprehensive test plan need to cover** that the
   per-module suites do not? Proposal: cross-vocabulary interference (a
   mention that matches in two vocabularies), the compositional path end
   to end, and a stress suite for RxNorm mirroring #53's. Anything else
   you would want measured before calling this finished?
