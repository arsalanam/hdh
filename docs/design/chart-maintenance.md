# Chart Maintenance — Closing the Review Loop (Draft)

**Where it lives:** `hdh.modules.ontology` (symptom coverage) ·
**`hdh.core.chartedit`** (amendment + audit — §7 Q6: core, not a module) ·
**Issues:** [#41](https://github.com/arsalanam/hdh/issues/41),
[#40](https://github.com/arsalanam/hdh/issues/40) ·
**Status:** BUILT — milestones A–C shipped; decisions inline (§7) · **Date:** 2026-08-16

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, review |
| | | |

Comprehension (PR #38) ships a review queue that **refuses to guess** —
anything it cannot ground goes to a human instead of the chart. Live
testing proved the refusal works and then walked straight into the two
things missing on either side of it:

1. **Too much enters review.** Symptom mentions have no ICD-10 billing
   mapping, so *every* note with a symptom queues an item that a human
   must handle. Signal drowns in noise (#41).
2. **Nothing can leave review.** There is no sanctioned path to resolve
   a review item into the chart, or to correct a mis-charted entry. The
   agent, asked to add the billing code, escalated to a raw SQL `INSERT`
   and the read-only guard refused it — correctly, and with nowhere to
   go next (#40).

Both are prerequisites for the milestone-E testing plan (#39): the
applier verdict matrix cannot be exercised while a whole verdict class
has no resolution path, and the scripted agent scenarios cannot assert
"charts cleanly" while ordinary notes always trip review.

---

## Contents

1. [Design principles this arc must honor](#1-principles)
2. [Part A — symptom billing coverage (#41)](#2-part-a)
3. [Part B — chart amendment and audit (#40)](#3-part-b)
4. [The join: resolving a review item](#4-join)
5. [What this unblocks for #39](#5-unblocks)
6. [Milestones](#6-milestones)
7. [Open questions — answer inline](#7-questions)

---

## 1. Design principles this arc must honor<a name="1-principles"></a>

The house rules, restated where they bite hardest here:

- **Explicit contracts.** Every mutation is a typed request object with a
  typed outcome — never positional booleans, never a bare dict.
- **Dependency injection.** The session, the clock, and the actor
  identity are injected; nothing constructs its own collaborators.
- **Strong types and immutables.** Frozen dataclasses for requests,
  outcomes, and audit records; enums for anything closed.
- **Encapsulation.** The chart-edit API is the only sanctioned mutation
  path; ORM rows are reached through it, not around it. Agent tools and
  CLI are both thin clients of the same core.
- **Single responsibility.** Coverage (Part A) is *data*; amendment
  (Part B) is *behavior*. They ship as separate units and meet only at
  §4.
- **Extensibility.** New amendable entities and new mapping sources plug
  in without touching existing code.

And the comprehension house rule still governs: **the LLM classifies,
deterministic code decides.** An agent may *propose* an amendment; the
edit API validates and records it.

## 2. Part A — symptom billing coverage (#41)<a name="2-part-a"></a>

### 2.1 Why symptoms miss today

The applier resolves a billing code by reverse `maps_to` lookup: given a
SNOMED concept, find an ICD-10-CM concept that maps to it. Those edges
come from PR #37 in three authority tiers — `PACK_AUTHORED` (condition
profiles), `CURATED_DEMO` (the hand map), `DERIVED_NORMALIZE` (funnel
output ≥ 0.6). All three are **disease-centric**, because they are
derived from the condition catalog, and the catalog contains diagnoses,
not complaints. A note saying *"occasional morning headaches"* links
correctly to SNOMED 25064002 and then dead-ends.

### 2.2 The source, and the licensing boundary

ICD-10-CM is public domain. SNOMED CT **concept identifiers** are safe to
ship (the release guard already permits tagged ids; it forbids catalog
*rows*). So a curated symptom map is shippable as source. UMLS crosswalk
files are **not** redistributable under our terms and must not be
vendored — they may inform curation, never be copied wholesale.

**Decision:** hand-curated, in-repo, reviewed like code. Roughly fifty
entries covering the R-chapter staples plus the highest-frequency
primary-care complaints.

### 2.3 Contracts

A new authority tier keeps provenance honest — these are *not* derived
and *not* the demo hand map:

```python
SYMPTOM_AUTHORITY = "CURATED_SYMPTOM"
```

Kept as its own constant rather than appended to `derive.py`'s
`AUTHORITIES`, because that tuple defines what
`record_maps_to_edges` **deletes and rebuilds wholesale**. Symptom edges
have a different lifecycle — they exist independently of what the
generator happened to put on a chart — so they get their own
rebuild-my-tier-only writer, and re-running `hdh ontology tag` cannot
wipe them.

That independence matters for a second reason: `derive_mappings` filters
its result to ICD-10 codes that actually appear on generated
`Condition` rows. Symptom codes deliberately do *not* appear there yet —
covering them is the whole point — so they must bypass that filter, and
they never participate in `tag_conditions`.

The map itself is data behind a source protocol, so a future RxNorm-style
terminology service can supply the same shape without edits here:

```python
@dataclass(frozen=True)
class SymptomMapping:
    icd10_code: str        # R51.9
    snomed_code: str       # 25064002
    display: str           # "Headache"
    note: str = ""         # why this pairing, for the reviewer

class MappingSource(Protocol):
    """Anything that can offer symptom-level ICD↔SNOMED pairings."""
    def mappings(self) -> tuple[SymptomMapping, ...]: ...
```

`CuratedSymptomSource` is implementation #1 (a frozen tuple in the
module). Edges are written by the existing `record_maps_to_edges`, which
is already idempotent per authority — so a rebuild replaces this tier
without touching the others.

### 2.4 Selection criteria

An entry earns its place only if all four hold:

1. the complaint is common in primary-care notes (the corpus is the
   arbiter, not intuition);
2. the ICD-10-CM code is genuinely billable as a *symptom* code (R
   chapter, or the specific-symptom codes elsewhere);
3. exactly one SNOMED concept is the obvious clinical equivalent —
   ambiguity means it is left out, not guessed;
4. the pairing survives a second reading with the `note` field
   explaining it.

### 2.5 Non-goals

This is **not** a general auto-mapper and must not become one. Anything
outside the curated set still refuses and queues for review — the
refuse-don't-guess posture is the product, not a limitation to be
engineered away. Coverage grows by review, one curated entry at a time.

## 3. Part B — chart amendment and audit (#40)<a name="3-part-b"></a>

> **Placement (Q6 answered: core).** Chart mutation is core behavior, so
> this lands in `hdh.core.chartedit` and the audit table is a **core
> model** in `hdh.core.models` with a core Alembic migration — not a
> schema-registry entity. Two consequences the design must respect:
> (1) core may not import from modules, so `chartedit` knows nothing
> about comprehension — the applier calls *into* it, never the reverse;
> (2) the agent tools live in `hdh.modules.agent`, which may import core
> freely. `hdh chart …` becomes a core CLI command in `hdh/cli.py`
> alongside the other core commands.

### 3.1 Scope: what is amendable

| Entity | Amend | Delete | Notes |
|---|---|---|---|
| `Condition` | status, controlled, icd10_code, description, dates | void | the review-resolution target |
| `Prescription` | dose, frequency, end/stop | void | stopping a med is an amend, not a delete |
| `Vital` | any measurement | void | transcription errors are the common case |
| `LabResult` | value, interpretation | void | |
| `Allergy` | severity, reaction, status | void | |
| `VisitNote` | — | — | **never mutated**: notes are the source record; corrections are addenda (comprehension already appends) |
| `Visit` | date, provider, type, chief complaint | **cascade void** | voiding a visit voids what it owns |

`Patient` demographics are deliberately out of scope for this arc —
identity edits are a different risk class and deserve their own review.

### 3.2 Contracts

```python
class EditAction(StrEnum):
    AMEND = "amend"
    VOID = "void"

class EditSource(StrEnum):
    CLI = "cli"
    AGENT = "agent"
    PIPELINE = "pipeline"     # the comprehension applier

@dataclass(frozen=True)
class Actor:
    """Who is making the change. Injected — never inferred."""
    name: str                 # "Dr. Priya Sharma, MD" | "arsalanam"
    source: EditSource
    provider_id: int | None = None

@dataclass(frozen=True)
class ChartEdit:
    """One proposed mutation of one chart row."""
    entity: str               # "Condition"
    row_id: int
    action: EditAction
    changes: Mapping[str, object] = field(default_factory=dict)  # amend only
    reason: str = ""          # required for clinical rows (§7 Q4)

@dataclass(frozen=True)
class EditOutcome:
    edit: ChartEdit
    applied: bool
    audit_id: int | None
    detail: str               # human-readable, agent-reportable
```

The public API is one function, injected everything, dry-run capable:

```python
def apply_edits(session, actor: Actor, edits: Sequence[ChartEdit],
                *, dry_run: bool = False) -> tuple[EditOutcome, ...]:
```

Per-entity knowledge lives behind a registry, so a new amendable entity
is a registration, not a new branch in a growing `if`:

```python
class AmendableEntity(Protocol):
    """What the edit API needs to know about one chart entity."""
    entity: str
    amendable_fields: frozenset[str]
    def load(self, session, row_id: int): ...
    def validate(self, row, changes: Mapping[str, object]) -> tuple[str, ...]: ...
    def void(self, session, row) -> None: ...
```

### 3.3 The audit log

A **core model** (Q6), append-only by construction — the API offers
insert and read, no update, no delete:

```python
class ChartAuditEvent(Base):
    """One recorded change to one chart row. Append-only: nothing in the
    codebase updates or deletes these rows."""

    __tablename__ = "chart_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_source: Mapped[EditSource] = mapped_column(SAEnum(EditSource), nullable=False)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False, index=True)
    entity: Mapped[str] = mapped_column(String(40), nullable=False)
    row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[AuditAction] = mapped_column(SAEnum(AuditAction), nullable=False)
    reason: Mapped[str] = mapped_column(String(400), default="")
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
```

`AuditAction` is `create | amend | void` — wider than `EditAction`
(`amend | void`) because the pipeline's own inserts are audited as
`create` (Q2), and creation is not something `apply_edits` performs.

`before`/`after` capture only the touched fields — a diff, not a row
snapshot, so the log stays readable and small. `patient_id` is
denormalized deliberately: "show me everything that ever happened to this
chart" must be one indexed query, not a join across seven entity tables.

**Voiding, not deleting.** Clinical records are voided (status flips,
`voided_at` set) so the audit trail keeps its referent. True row removal
stays an explicit administrative operation outside this API — the dev-DB
cleanup we have been doing by hand (§7 Q1).

### 3.4 CLI surface

```bash
hdh chart history --mrn MRN...                    # the audit trail, newest first
hdh chart amend --entity Condition --id 42 --set status=RESOLVED --reason "..."
hdh chart void  --entity Prescription --id 17 --reason "entered in error"
hdh chart void  --visit 2064 --reason "duplicate encounter" --dry-run
```

Every mutating command supports `--dry-run` and prints the same outcome
lines the agent would report.

### 3.5 Agent tools, and their guardrails

Three tools, all `@guard`-wrapped (session rollback on failure), all
returning the typed outcomes as JSON:

- `amend_chart_entry` — one row, explicit fields, requires a reason;
- `void_chart_entry` — one row or one visit, requires a reason;
- `chart_history` — read-only, the audit trail for a patient.

Guardrails, stated as contracts rather than hopes:

1. **One row per call.** No bulk operations, no predicates — an agent
   cannot void "all conditions where…".
2. **Reason required.** The tool schema makes it non-optional; an empty
   reason is a validation failure, not a default.
3. **Preview before write.** The agent is instructed to call with
   `dry_run` first for anything it did not itself just create, and to
   report the preview to the user before applying.
4. **Never delete.** Void only; hard deletion is not exposed to the
   agent at all.
5. **Audit is not optional.** There is no code path that mutates without
   writing the event — the audit write happens inside the same
   transaction as the change.

### 3.6 What this does *not* do

No undo stack, no versioned time-travel of the whole chart, no
approval-workflow state machine. The audit log records what happened;
reconstructing a prior state from `before` diffs is possible but is not
a feature this arc ships.

## 4. The join: resolving a review item<a name="4-join"></a>

Today `hdh comprehend --review --resolve ID --decision accept` marks a
record complete and **writes nothing** — accepting the item does not
chart it. With Part B in place, resolution becomes a real transition:

```
review item ("headaches", SNOMED 25064002, no billing map)
   ├─ accept --icd10 R51.9   → creates the Condition via the edit API,
   │                            audit event action=create, source=cli,
   │                            reason="review resolution: record #12"
   ├─ accept                 → charts it when coverage (Part A) now maps it
   └─ reject --reason "..."  → record failed, audit event records the refusal
```

The resolution path is the *only* new writer, and it goes through
`apply_edits` like everything else — so a review item resolved by a human
in the CLI and an amendment made by the agent land in the same trail with
the same shape.

## 5. What this unblocks for #39<a name="5-unblocks"></a>

- **Applier verdict matrix**: `review` becomes a terminal state with a
  tested exit, so every verdict class is exercisable end to end.
- **Scripted agent E2E**: the guardrail probe gets a positive twin — the
  agent is *refused* raw SQL and *succeeds* through the sanctioned tool,
  which is the scenario that actually documents the boundary.
- **Signal-to-noise**: with symptom coverage, a normal note charts
  cleanly, so a review item appearing in a test means something.
- **Idempotency testing**: re-running a note after an amendment has a
  defined expected outcome (`confirmed`, not a duplicate).

## 6. Milestones<a name="6-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **A** ✅ | Part A: `SymptomMapping` + `MappingSource` protocol, the curated set, `CURATED_SYMPTOM` authority tier, wired into `hdh ontology tag`; tests that a symptom note charts without review | coverage is data, provenance stays honest, refuse-don't-guess survives for everything uncurated |
| **B** ✅ | Part B core: contracts, `apply_edits`, the `AmendableEntity` registry for the §3.1 entities, the `ChartAuditEvent` registry entity + migration, `hdh chart history/amend/void` with `--dry-run` | one sanctioned mutation path; every change auditable; voiding beats deleting |
| **C** ✅ | Part B agent surface + the §4 join: three guarded tools, review resolution writing through the edit API, docs/guide updates | the loop closes — the agent can fix what it charted, and a human can resolve what it refused |

Each milestone is human-tested before the next begins, per the usual
rhythm, and the whole arc ships as one PR. **All three delivered.**

### As built — where the design bent

Four things the build learned that the design did not know:

1. **Symptom edges own their tier** (§2.3, already folded in): appending
   `CURATED_SYMPTOM` to `derive.py`'s `AUTHORITIES` would have let
   `record_maps_to_edges` delete them on every re-run, and
   `derive_mappings`' filter would have dropped codes that appear on no
   generated Condition — which is every symptom code.
2. **Voiding needed a visibility rule.** A `voided_at` column alone does
   nothing: exports, cohort queries and comprehension's own reconciliation
   would still see the row. One loader criterion on the Session
   (`chartedit/visibility.py`) hides voided rows from ORM reads, with
   `include_voided=True` as the deliberate opt-in. Two limits are
   documented rather than hidden: Core-table reads bypass the ORM, and a
   row already in a session's identity map stays reachable in that
   session.
3. **Postgres keeps ENUM types when a table is dropped**, so migration
   0005 creates with `checkfirst=True` and drops the types in
   `downgrade` — otherwise a downgrade/upgrade cycle dies on
   `DuplicateObject`. Found by testing the migration against a database
   stamped at 0004, not by reading it.
4. **Attribution reads the reason text.** `Dr. Priya Sharma, MD` has to
   match a provider writing "confirmed by Dr. Sharma", so the matcher
   accepts full name or surname on word boundaries. Until authentication
   lands (§7 Q3), this is the whole of actor identity for agent edits.

## 7. Open questions — answer inline<a name="7-questions"></a>

1. **Void vs hard delete.** Proposal: clinical rows are *voided* (never
   removed) so the audit trail keeps its referent; genuine deletion stays
   an admin path outside this API — which also means our by-hand dev-DB
   cleanups get a supported command (`hdh chart purge-visit --id N
   --yes`, gated and never exposed to the agent). Agree, or do you want
   real deletes available to the CLI as a first-class operation? agree

2. **Audit scope.** Proposal: every mutation through `apply_edits`, plus
   the comprehension applier's writes (`source=pipeline`) so a chart's
   history shows *how* each entry arrived. Generation is excluded — that
   is dataset creation, not chart maintenance. Should the applier be in
   or out? in

3. **Actor identity.** Proposal: an injected `Actor(name, source,
   provider_id)` — the CLI uses the OS user, the agent uses the provider
   named in the request (falling back to `"agent"`), the pipeline uses
   the note's author. Is provider-level attribution enough, or do you
   want a real user/account concept first? ..real user will come latter when we creat authentication and 
   for now provider name and back up of agent is good enough

4. **Reason required?** Proposal: mandatory for clinical rows (conditions,
   prescriptions, labs, allergies, visits), optional for vitals
   transcription fixes. Or mandatory everywhere, no exceptions?
   mandatory for clinical rows
5. **Symptom set size.** Proposal: ~50 curated entries covering the
   R-chapter staples and the top primary-care complaints, chosen against
   the generated corpus's actual symptom vocabulary. Bigger (100+, more
   coverage, more review burden) or tighter (25, only what the corpus
   proves)? around 50 ok

6. **Where does Part B live?** Proposal: a new module
   `hdh.modules.chartedit`, so core stays untouched and the audit entity
   arrives through the schema registry like every other module entity.
   Alternative: `hdh.core.chartedit`, arguing that chart mutation is core
   behavior rather than an optional feature. Module or core?
   core
