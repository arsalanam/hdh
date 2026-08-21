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
10. [Open questions — answer inline](#10-questions)

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
| **M1** | `hdh.core.termsearch`: the funnel, the coverage rule, the ceilings, a `SearchProfile` contract. SNOMED delegates to it. Shared UTS acquisition. | the #53 ratchet does not move |
| **M2** | LOINC delegates too; `_covers_every_word` import deleted; a quality-gate check for cross-module imports | no module reaches into another's internals |
| **M3** | `hdh.modules.rxnorm`: loader (RRF), graph edges, `SearchProfile`, `OntologyService` #4, fabricated fixture | a drug vocabulary lands with no funnel of its own |
| **M4** | Compositional coding: ingredient → strength → form, coded at the deepest supported level; `Prescription`/`MedicationStatement` gain code columns; `hdh rxnorm code` | "Lisinopril 10mg once daily" resolves to an SCD, and says why |
| **M5** | Agent tools; the comprehensive test plan across all three vocabularies | the interface holds under a conversation |

Each milestone is human-tested before the next begins.

## 10. Open questions — answer inline<a name="10-questions"></a>

1. **Is the retrofit worth doing before RxNorm?** Proposal: yes — M1 and
   M2 first, so RxNorm never grows a funnel to remove later. The cost is
   two milestones before any new capability lands. The alternative is to
   build RxNorm on the current shape and retrofit all three afterwards,
   which is cheaper now and more expensive at every later step. Retrofit
   first, or capability first?

2. **Where should `termsearch` live — `hdh.core` or its own module?**
   Proposal: `hdh.core`, because three modules depend on it and core is
   what modules are allowed to depend on. Against: it is a retrieval
   strategy rather than a chart concept, and core has so far meant "the
   chart". Core, or `hdh.modules.termsearch` with the ontology modules
   depending on it explicitly?

3. **Which RxCUI level do we store when the evidence is partial?**
   Proposal: the deepest level supported — SCD when strength and form are
   both known, SCDC with strength alone, IN when only the drug is named —
   with the level recorded so a reader can see what was inferred.
   Alternative: always the ingredient, and treat strength/form as
   attributes of the request rather than part of the code. Deepest, or
   always-ingredient?

4. **Should the condition catalog's `RxSpec` carry an RXCUI?** It would
   make generated prescriptions codeable with no comprehension involved,
   and turn the formulary from strings into drugs. It also means the
   catalog can no longer be authored without a licensed release to check
   against. In scope for M4, or its own arc?

5. **Brands.** Proposal: load SBD/BN and resolve *to* them when a note
   names a brand ("Zestril"), but code the request at the clinical-drug
   level with the brand recorded in `detail`. Or should a branded order
   carry the branded RXCUI, since that is what was actually prescribed?

6. **How far does the shared funnel go?** Proposal: `termsearch` owns
   every rung including a future dense one, and comprehension owns
   context-aware reranking because only it holds the sentence (§3). Is
   that the right seam, or should reranking also be shared so the agent
   gets it too?

7. **What does the comprehensive test plan need to cover** that the
   per-module suites do not? Proposal: cross-vocabulary interference (a
   mention that matches in two vocabularies), the compositional path end
   to end, and a stress suite for RxNorm mirroring #53's. Anything else
   you would want measured before calling this finished?
