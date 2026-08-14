# SNOMED CT Module — Design

**Module:** `hdh.modules.snomed` · **Status:** IMPLEMENTED (milestones A–D, 2026-08-13/14) ·
**Date:** 2026-08-11 · **Parent design:** [notes-comprehension-service.md](notes-comprehension-service.md) §10

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, licensing analysis, top-down design |
| | | |

The same principles that built the ICD-10-CM module, applied to a harder
ontology: **licensed data** (loader ships, data never does), a **DAG**
instead of a tree, **1.6M synonym descriptions** as first-class rows, and
**attribute relationships** that carry the clinical semantics interventions
need (thrombectomy = *method:* removal + *site:* cerebral artery).

---

## Contents

1. [Scope and consumers](#1-scope-and-consumers)
2. [Data sources, licensing, and logistics](#2-data-sources-licensing-and-logistics)
3. [Schema: what lands where](#3-schema-what-lands-where)
4. [First-time load pipeline](#4-first-time-load-pipeline)
5. [Periodic updates](#5-periodic-updates)
6. [Graph store: closure and attributes](#6-graph-store-closure-and-attributes)
7. [Retrieval: the OntologyService implementation](#7-retrieval-the-ontologyservice-implementation)
8. [Encapsulation retrofit (master doc §5)](#8-encapsulation-retrofit-master-doc-5)
9. [Testing and fixtures — the licensing twist](#9-testing-and-fixtures--the-licensing-twist)
10. [Milestones](#10-milestones)
11. [Open questions](#11-open-questions)

---

## 1. Scope and consumers

**In scope (v1):** the SNOMED CT **US Edition** — all active concepts
(~360k), descriptions (~1.6M), is-a hierarchy, and **defining attribute
relationships** (finding-site, method, causative-agent, associated-
morphology, using-device/substance, …). Attributes are promoted to v1
because interventions demand them: *"mechanical thrombectomy of cerebral
artery"* is clinically meaningful through its attributes, not its name —
and both the neurology specialty pack and care-plan intervention
codification consume exactly that.

**Consumers:** the comprehension service (problems, findings, procedures —
its primary normalize() target), the care-plan module (SNOMED-coded
concerns and interventions per MCC eCare Plan), the agent (`snomed` tools),
and the ICD module (curated `maps_to` edges, issue #18).

**Out of scope (v1), explicitly:** postcoordination (composing new
concepts from SNOMED's compositional grammar) — precoordinated concepts
only; ICD↔SNOMED map loading (issue #18, separate); non-US editions
(structure is identical RF2; a `--edition` flag is future-proofing, not a
commitment).

## 2. Data sources, licensing, and logistics

The load-bearing difference from ICD-10-CM: **CMS files are public domain;
SNOMED CT is licensed.** Everything downstream respects that line.

| Item | Reality                                                                                                                                                                                                                                                                     |
|---|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Source | SNOMED CT US Edition, RF2 format, via NLM's UMLS Terminology Services (UTS)                                                                                                                                                                                                 |
| License | Free **UMLS Metathesaurus License** — individual signs up at uts.nlm.nih.gov (the US is a SNOMED International member, so US affiliate use is no-cost); approval is days, with an annual usage-report attestation to keep it active.Hdh will never ship with SNOMED CT data |
| Credential | a personal **UTS API key** (from the UTS profile page) enables scripted download — `UMLS_API_KEY` in `.env` (gitignored; `.env.example` documents it, `just check-env` verifies it). *Implementation note (milestone C): the vendor-key convention `UMLS_API_KEY` was adopted, matching `ANTHROPIC_API_KEY`; `HDH_UMLS_API_KEY` is accepted as a fallback.* |
| Cadence | US Edition publishes **twice yearly (March 1 / September 1)**                                                                                                                                                                                                               |
| Redistribution | **prohibited** — hdh ships the loader and a synthetic fixture (§9), never RF2 content; the release zip is cached per-user in `~/.hdh/snomed/<release>/`, exactly like the ICD cache but sourced from the user's own credential                                              |
| Non-US users | member-country affiliates license via their national release center; the loader takes `--source <dir>` so any legitimately obtained RF2 loads                                                                                                                               |

RF2 Snapshot files consumed (v1): `sct2_Concept_Snapshot` (activity,
definition status), `sct2_Description_Snapshot` (FSN + synonyms),
`sct2_Relationship_Snapshot` (is-a + attributes), and the **US English
language refset** (`der2_cRefset_LanguageSnapshot`) to select each
concept's preferred term. Full/Delta files are not consumed in v1 (§5).

## 3. Schema: what lands where

Same shared tables, new declarations — all via the schema registry, all
JSON, per the house pattern:

| Piece | Landing | Notes |
|---|---|---|
| Concepts | `ontology_concepts`, `ontology='snomed_ct'`, `code=<SCTID>`, `id='snomed_ct:<SCTID>'` | `display` = US-preferred term; FSN kept in `properties.fsn` |
| Kind | `kind='concept'` (one new enum value, one Alembic migration) | the **semantic tag** — *(disorder)*, *(procedure)*, *(substance)*… parsed from the FSN — goes to `properties.semantic_tag`; routing logic prefers top-level-hierarchy ancestry over tag string-parsing where both exist |
| Hierarchy | N `parent_of` edges (inverted is-a) | **`path` and `hierarchy_depth` are NULL** — DAG; tree conveniences are ICD-private (master doc §5) |
| Attributes | `edge_type='attribute'` (one new enum value) + `properties.attribute` = `{"type_id": ..., "name": "finding_site"}` | one generic edge type covers all ~50 attribute types without enum churn; hot attributes get a partial index |
| Descriptions | **NEW entity `OntologyTerm`** (`ontology_terms`): concept FK, `term`, `term_type` (fsn / preferred / synonym), `language`, `active` | ~1.6M rows; the synonym index the comprehension funnel searches — ICD retro-gains a row per short/long description later |
| Closure | **NEW entity `OntologyClosure`** (`ontology_closure`): `ancestor_id`, `descendant_id`, `min_depth` | ~10M+ rows, composite PK/indexes both directions; **private to this module's service** — no other module queries it (§8) |
| Versioning | `effective_fy` reused as release tag `YYYYMM` (e.g. 202603); inactive concepts get `retired_fy`, never deleted | SNOMED `effectiveTime` preserved in `properties` |

## 4. First-time load pipeline

The `LoadStage` protocol, reused verbatim — new stages, same contract
(fail loudly before `finalize`; ledger row only on full success):

```
 1 acquire       UTS download via HDH_UMLS_API_KEY (or --source dir);
                 release-zip checksum into the ledger
 2 parse         RF2 tab-separated snapshots → typed row streams
                 (concepts / descriptions / relationships / langrefset)
 3 build         active concepts + preferred-term resolution + semantic
                 tags; description rows; edge rows (is-a → parent_of,
                 attributes → attribute edges)
 4 load          COPY-batched inserts: ~360k concepts, ~1.6M terms,
                 ~1.5M+ edges
 5 closure       compute is-a transitive closure in memory (array-based
                 BFS over ~1.5M edges) → bulk load ~10M closure rows
 6 accelerate    (PostgreSQL) FTS + trigram GIN over ontology_terms.term;
                 partial index on hot attribute types
 7 verify        invariants: root concept 138875005 present; every active
                 concept has an FSN and a preferred term; closure is
                 consistent (acyclicity is implied by closure terminating;
                 spot golden concepts §9); counts within tolerance of the
                 release's published stats
 8 finalize      ledger row: edition, release date, checksums, counts,
                 duration
```

**Targets (to be benched, not asserted):** full US Edition load under
15 minutes on PostgreSQL via `just deps`; closure computation under 5
minutes. SQLite is not a target — this module is where the retirement
plan's "PostgreSQL-only for new modules" line is drawn (design
icd10cm §5.3 lineage).

## 5. Periodic updates

Biannual, non-destructive — the ICD fiscal-year playbook:

- v1 strategy: **full snapshot reload per release** (`--force` semantics),
  with continuity rules: concepts absent-or-inactivated get `retired_fy`
  set (rows never deleted — `NoteMention` and `maps_to` FKs stay valid);
  re-activated concepts clear it; description/edge sets are rebuilt per
  release; closure is recomputed (it is derived data, always safe to
  rebuild).
- RF2 **Delta** files and historical-association refsets (SAME-AS,
  REPLACED-BY → edges guiding consumers off retired concepts) are the v2
  refinement, worth doing only if reload time or churn tracking proves
  painful — measured first.
- Each release is a ledger row; `hdh snomed status` shows the active
  release and retired-concept counts.

## 6. Graph store: closure and attributes

The two structures that make this module more than a bigger ICD:

**Closure** answers the questions comprehension and care logic ask
constantly — *subsumption*: "is this concept a kind of diabetes /
cerebrovascular disorder?" One indexed lookup: `(ancestor, descendant) ∈
closure`. Descendant sweeps ("everything under |Cerebrovascular
disease|") are one range scan. No recursive CTE at query time; the CTE
runs once per release inside the loader.

**Attribute edges** carry intervention semantics: thrombectomy's
`method → |Removal|`, `procedure site → |Cerebral artery structure|`;
a disorder's `finding site` and `causative agent`. v1 loads them all
(generic edge type, §3); the *retrieval* API exposes them simply
(`attributes(code) → {name: [concepts]}`) and defers clever attribute
*querying* (e.g., "all procedures with site under X") until a consumer
actually needs it — that query is closure-join-composable when the day
comes.

## 7. Retrieval: the OntologyService implementation

The first full implementation of the master doc's protocol — proving it on
the hard case:

| Protocol method | SNOMED implementation |
|---|---|
| `lookup(code)` | PK fetch + preferred term + semantic tag + FSN |
| `ancestors(code)` / `descendants(code)` | closure joins (SET semantics native) |
| `synonyms(code)` | `ontology_terms` rows |
| `normalize(mention, context)` | the ICD funnel shape over the terms index: FTS (progressive relaxation) → trigram fuzz → candidates ranked by term-match quality, semantic-tag fit to the mention type, and ancestor-set context; SapBERT slots here later per master doc §6, bench-gated |
| extra: `subsumes(a, b)` | one closure hit — exposed because comprehension's disambiguator and care-gap rules both want it cheap |

CLI mirror: `hdh snomed load / status / lookup / search / subsumes /
attributes / bench`. Agent tools follow the icd pattern (published
`build_snomed_tools`), gated on catalog presence.

## 8. Encapsulation retrofit (master doc §5)

Sequenced **with** this module, not after it: the `OntologyService`
protocol lands first (icd10cm retrofitted as implementation #1);
`patterns._descend` dispatches per ontology; the quality gate gains the
no-`path`-outside-owner check; tree helpers raise `UnsupportedHierarchy`
for DAG ontologies; the SQL tool's schema note marks `path` ICD-only.
Definition of done for milestone A includes all five items — the silent-
empty failure class must be closed *before* the first DAG rows exist.

## 9. Testing and fixtures — the licensing twist

ICD's fixture was a slice of public-domain data. **SNOMED's cannot be** —
a real RF2 extract in a public repo is redistribution. So the committed
fixture is **synthetic RF2**: structurally perfect files (real column
layouts, valid SCTID check-digits and partition identifiers, coherent
is-a/attribute graph) with **fabricated concepts** — a mini clinical world
(a disorder tree, a procedure tree with method/site attributes echoing the
thrombectomy shape, synonyms, a language refset). Tests prove the loader,
closure, DAG semantics, and normalize() end-to-end with zero licensed
content. Full-release property tests (`@pytest.mark.fullload`) and golden
concepts (|Diabetes mellitus|, |Cerebrovascular accident|, a thrombectomy
procedure) run only on machines where a licensee has loaded the real
edition; CI never touches licensed data. Bench harness identical in shape
to `hdh icd bench` with closure-specific patterns (subsumption,
descendant sweep).

## 10. Milestones

| | Delivers | Proves | Status |
|---|---|---|---|
| **A** | OntologyService protocol + full §8 retrofit; `OntologyTerm` + `OntologyClosure` entities; enum migrations (kind `concept`, edge `attribute`) via Alembic | encapsulation closed before DAG rows exist; registry v2 handles the new entities | ✅ done |
| **B** | RF2 parser + full pipeline on the synthetic fixture; `hdh snomed` CLI | DAG load, closure, preferred terms — offline, license-clean | ✅ done |
| **C** | UTS download; full US Edition load + closure + bench on `just deps` | scale reality (386k concepts / 1.03M terms / 7.77M closure rows loaded and benched) | ✅ done |
| **D** | `normalize()` funnel over terms + agent tools + `subsumes`; ICD term retro-load (Q2) | the comprehension service's primary dependency is ready; agent answers cohort questions by graph semantics | ✅ done |

*Implementation notes:* `OntologyTerm` ownership landed in the icd10cm
module (which already owns the shared ontology tables) so the ICD term
retro-load never inverts the dependency direction; `OntologyClosure`
stays snomed-private as designed. Bulk loads use PostgreSQL COPY
(psycopg3). The agent's tools ship as `build_snomed_tools` — a published
API mirroring `build_icd_tools`, catalog-gated.

After review, the master doc's §10 placeholder collapses to a pointer at
this document, and comprehension drill-down resumes at "what is a mention"
with its hardest dependency secured.

## 11. Open questions

1. **Load scope trim?** All 19 top-level hierarchies vs the clinical core
   (clinical finding, procedure, body structure, substance, pharmaceutical,
   situation, observable entity) — full is simpler and honest;
   trimming saves ~30% rows. Lean full; challenge welcome.
2. **`ontology_terms` for ICD too, now or later?** Retro-loading ICD
   descriptions as term rows unifies the funnel's search surface before
   comprehension arrives — probably milestone D, cheap.
3. **Closure storage**: plain table (loader-owned, portable) vs
   PostgreSQL matview (refresh semantics) — plain table favored; the
   loader owns derived data explicitly.
4. **Inactive concepts in normalize()**: excluded from candidates, but
   old notes reference them — include with a `retired` flag and a
   historical-association pointer (v2)?
5. **Preferred-term language**: US English refset only in v1 — is that
   acceptable for the synthetic corpus, or does the fixture need to prove
   multi-language selection works?. V1 US English

## 12. Deferred: vector search (SapBERT + pgvector) — trigger recorded

Bench-gated per the master doc §6 and RFC Q10. The trigger: once the
comprehension service produces real mentions, measure the lexical
funnel's recall@k against them; the miss list (surface-form gaps like
"heart attack" → *Myocardial infarction* with no bridging synonym row)
is the benchmark that justifies embeddings — not before.

Shape when it lands: SapBERT (BERT-base, trained on UMLS synonym pairs —
exactly this mention→concept task) embedding per term row, pgvector HNSW
index owned by the accelerate stage, hybrid-ranked as one more funnel
stage behind the same `normalize()` contract. CPU feasibility (measured
expectations, 2026-08-13): query-time encode ~10–50 ms (ONNX int8);
one-time bulk embed of 1.6M terms ~2–5 h per biannual release (cached);
HNSW serving RAM ~5 GB float32 / ~2.5 GB float16. No GPU required.
