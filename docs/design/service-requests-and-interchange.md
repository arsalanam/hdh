# Service Requests and External Interchange (Draft)

**Where it lives:** `hdh.core` (the request entity) ·
`hdh.modules.interchange` (mock external labs and pharmacies) ·
**Follows:** [notes-comprehension-service.md](notes-comprehension-service.md) §13 phase 3,
[comprehension-extraction-schema.md](comprehension-extraction-schema.md) §10.3 ·
**Status:** DRAFT — open RFC ([#52](https://github.com/arsalanam/hdh/issues/52)) · **Date:** 2026-08-17

### Contributors

| Name | Role | Contribution |
|---|---|---|
| Ajmal Mahmood | Author / Architect | Requirements, review; the requests-before-coding sequencing, and OMOP CDM as the completeness lens |
| | | |

The chart records **what happened** and has no way to record **what was
asked for**. That one gap explains several unrelated-looking problems we
have hit, and it must close before ontology coding for medications and
labs is worth building — a code needs something to code.

Hence the deliberate order:

1. **Persist requests** — medication, lab, referral, procedure — as
   first-class entities with a lifecycle.
2. **Then interchange** — export them to mock external labs and
   pharmacies, and import results back onto the chart.
3. **Then coding** — RxNorm and LOINC modules, serving real request
   records rather than existing for their own sake.

**OMOP CDM is used here as a completeness reference, not a structural
one** (§3). We are not adopting its schema — hdh stays FHIR-shaped and
ORM-native — but OMOP is the most rigorous published answer to *what
fields a clinical fact needs to remain analysable years later*, and
checking our model against it found real omissions.

---

## Contents

1. [The evidence that this gap is real](#1-evidence)
2. [Contracts: the ServiceRequest entity](#2-contracts)
3. [Completeness check against OMOP CDM](#3-omop)
4. [What changes for the existing tables](#4-existing)
5. [How comprehension fills it](#5-comprehension)
6. [Interchange: mock labs and pharmacies](#6-interchange)
7. [Where ontology coding lands](#7-coding)
8. [Milestones](#8-milestones)
9. [Open questions — answer inline](#9-questions)

---

## 1. The evidence that this gap is real<a name="1-evidence"></a>

Not speculation — five places in the current codebase are bent around it:

| Symptom | Where | What it really is |
|---|---|---|
| `RxSpec("Lifestyle counseling referral", "Referral", "—", "—")` and `RxSpec("Rest & fluids", …)` sit in a condition's **formulary** | `core/disease_engine.py` | A referral and a piece of advice modelled as drugs, because there is no request entity. The eval had to learn to ignore them (#49). |
| `LabResult` exists only *with* a value | `core/models.py` | No "ordered, awaiting result" state — a pending lab is unrepresentable. |
| "basic metabolic panel to be drawn before that visit" is comprehended correctly, then dropped | comprehension §10.3 | A documented gap: orders have nowhere to land. |
| `assemble.py` emits `MedicationRequest` resources | comprehension stage 6 | Synthesised per export from a mention — no persisted request, so nothing can track a lifecycle or match a result to it. |
| `Visit.follow_up_days` is an integer | `core/models.py` | The simplest possible order ("come back in 90 days") flattened into a scalar. |

## 2. Contracts: the ServiceRequest entity<a name="2-contracts"></a>

One table with a `kind` discriminator, not four. The kinds share a
lifecycle, an authoring visit, a requester, a code and a fulfilment link;
splitting them duplicates all of that to gain nothing. (FHIR models
`MedicationRequest` separately from `ServiceRequest`; §9 Q1 asks whether
to mirror that — the emitter can map one table onto two resource types
either way.)

```python
class ServiceKind(str, enum.Enum):
    MEDICATION = "medication"   # -> FHIR MedicationRequest
    LAB = "lab"                 # -> FHIR ServiceRequest
    REFERRAL = "referral"       # -> FHIR ServiceRequest
    PROCEDURE = "procedure"     # -> FHIR ServiceRequest
    FOLLOW_UP = "follow_up"     # the scalar that wants to be an order


class RequestStatus(str, enum.Enum):
    """FHIR request lifecycle, trimmed to states we can actually reach."""
    DRAFT = "draft"                       # comprehended, not yet released
    ACTIVE = "active"                     # sent / awaiting fulfilment
    COMPLETED = "completed"               # result or dispense received
    REVOKED = "revoked"                   # cancelled by a human
    ENTERED_IN_ERROR = "entered_in_error" # voided via chartedit


class RequestOrigin(str, enum.Enum):
    """WHERE the record came from — OMOP's *_type_concept lesson (§3)."""
    GENERATED = "generated"     # the synthetic generator
    COMPREHENSION = "comprehension"  # extracted from a note
    AGENT = "agent"
    CLINICIAN = "clinician"     # entered directly via CLI/UI
    EXTERNAL = "external"       # arrived from a partner
```

```python
class ServiceRequest(Base):
    """Something the chart ASKED FOR: a drug, a panel, a referral, a
    procedure, a return visit."""

    id: Mapped[int]
    patient_id: Mapped[int]                  # FK — the subject
    visit_id: Mapped[int | None]             # FK — the authoring encounter
    requester_id: Mapped[int | None]         # FK providers.id
    kind: Mapped[ServiceKind]
    status: Mapped[RequestStatus]
    origin: Mapped[RequestOrigin]            # provenance (§3)
    display: Mapped[str]                     # "Basic metabolic panel"
    code_system: Mapped[str | None]          # "loinc" | "rxnorm" | "snomed_ct"
    code: Mapped[str | None]                 # None until a coder resolves it
    reason_condition_id: Mapped[int | None]  # FK — the TREATS link, persisted
    requested_date: Mapped[date]
    occurrence_date: Mapped[date | None]     # "before the next visit"
    end_date: Mapped[date | None]            # explicit, not derived (§3)
    quantity: Mapped[float | None]           # (§3)
    route: Mapped[str | None]                # (§3)
    sig: Mapped[str | None]                  # verbatim directions (§3)
    stop_reason: Mapped[str | None]          # why it ended early (§3)
    detail: Mapped[dict | None]              # kind-specific extras
    voided_at: Mapped[datetime | None]       # chartedit
```

Three deliberate choices:

- **`code` is nullable.** A request is real before it is coded — that is
  the point of ordering it. An uncoded request is legitimate state, not
  an error, and it is exactly what the RxNorm/LOINC modules will later
  fill in. Refuse-don't-guess: we never invent a code to satisfy a column.
- **`reason_condition_id` persists the TREATS relation.** Comprehension
  already derives "Lisinopril **for hypertension**" (§4) and currently
  discards it after FHIR export. This is where it lands.
- **`detail` is JSON for the long tail only.** The fields OMOP shows are
  load-bearing (§3) are real columns; genuinely kind-specific extras
  (panel members, referral specialty) stay in JSON rather than four
  kinds' worth of mostly-NULL columns.

## 3. Completeness check against OMOP CDM<a name="3-omop"></a>

OMOP is the reference for **what a clinical fact needs to stay
analysable**, not for how to lay it out. Read against
`DRUG_EXPOSURE`, `MEASUREMENT`, `PROCEDURE_OCCURRENCE`, `SPECIMEN`,
`FACT_RELATIONSHIP` and `NOTE_NLP`, our model was missing six things —
four of which we adopt now, two we record as deliberate deferrals.

| OMOP field(s) | What it captures | hdh today | Decision |
|---|---|---|---|
| `drug_type_concept_id`, `measurement_type_concept_id`, `condition_type_concept_id` | **Provenance of every clinical row** — EHR order vs claim vs patient-reported vs derived | Only in `chart_audit_events`, never on the row | **Adopt** as `ServiceRequest.origin`. A row that cannot say where it came from cannot be trusted in analysis, and we already learned this lesson in the audit trail. |
| `quantity`, `days_supply`, `sig`, `route`, `stop_reason` | What was actually dispensed and how it was taken; why it ended | `dose`, `frequency`, `duration_days`, `refills` only | **Adopt** `quantity`, `route`, `sig`, `stop_reason`. `sig` matters most: the verbatim direction line is what a pharmacy reads, and comprehension already captures it as attributes. |
| `drug_exposure_start_date` **and** `_end_date` | Explicit interval | `duration_days` only | **Adopt** `end_date`. A derived end cannot express "stopped early", which is precisely what `stop_reason` accompanies. |
| `value_as_number` **and** `value_as_concept_id`, `operator_concept_id`, `value_source_value` | Results that are **not numeric**: "positive", "no growth", "<0.01" | `LabResult.value` is a `float` — a urine culture or a qualitative panel is unrepresentable | **Adopt** on `LabResult` (§4). This is the most consequential omission OMOP surfaced: whole classes of real lab result cannot currently be stored at all. |
| `SPECIMEN` (source, collection date) | What was collected, when | absent | **Defer, documented.** The mock lab round trip does not need it; a real integration would. Noted so its absence is a decision, not an oversight. |
| `FACT_RELATIONSHIP` | Generic typed link between any two facts | `reason_condition_id` is a single special case | **Defer.** Our one real need (TREATS) is served by the FK; a generic relationship table earns its place when a second relation kind lands (comprehension §4.1 reserves `MEASURES`/`REVEALS`). |

One pleasant confirmation rather than a gap: OMOP's **`NOTE_NLP`**
(`lexical_variant`, `offset`, `term_exists`, `term_temporal`,
`term_modifiers`, `note_nlp_concept_id`) is close to a field-for-field
match with our `NoteMention` — verbatim text, span offset, assertion,
attributes, resolved concept. The comprehension model is already
CDM-shaped where it counts, which is a good sign for exporting an OMOP
view later if anyone wants one.

## 4. What changes for the existing tables<a name="4-existing"></a>

**`Prescription` stays.** It is the *dispense/administration* record —
what the patient is actually on — and `MedicationStatement` plays the
cross-visit role. A medication `ServiceRequest` is the **order**; a
`Prescription` is what came back. They gain a link, not a merge:

```
ServiceRequest(kind=medication, status=active)
        │  fulfilled_by
        ▼
Prescription(visit_id=…, drug_name=…)      # unchanged shape
```

**`LabResult` gains `request_id` and non-numeric results.** Per §3:

```python
    request_id: Mapped[int | None]      # what was ordered
    value: Mapped[float | None]         # unchanged
    value_text: Mapped[str | None]      # "positive", "no growth"
    comparator: Mapped[str | None]      # "<", ">" for "<0.01"
```

Today a result appears from nowhere and must be numeric. With a request
it can be matched to what was ordered — and an *unmatched* result becomes
visible rather than silently landing.

**`Visit.follow_up_days` stays** (generator convenience) but a
`FOLLOW_UP` request is emitted alongside, so the agent can see and amend
a return-visit order like any other.

**Migration:** additive throughout. One new table, nullable columns on
two existing ones, inspector-guarded like migration 0005. Existing rows
keep working: `request_id IS NULL` means "generated before requests
existed", and `origin` backfills to `GENERATED`.

## 5. How comprehension fills it<a name="5-comprehension"></a>

The applier already produces verdicts for conditions, medications, vitals
and allergies. Requests are a fifth pass over the same comprehended note,
and the plan section is where they live:

| Note text | Request |
|---|---|
| "Start Lisinopril 10mg once daily" | MEDICATION, `sig` verbatim, `route`/`quantity` from attributes, `reason_condition_id` from TREATS |
| "basic metabolic panel to be drawn before that visit" | LAB, `occurrence_date` from the follow-up |
| "Refer to cardiology" | REFERRAL, `detail={specialty}` |
| "Return in 30 days" | FOLLOW_UP, `occurrence_date = visit + 30d` |

Same discipline as everything else: a request that cannot be grounded
gets a `review` verdict rather than a guessed code, every write goes
through the audited path (`record_creation`, `source=pipeline`), and
`origin=COMPREHENSION` records where it came from.

This also retires the `rx_options` abuse: `Lifestyle counseling referral`
becomes a REFERRAL request, and `Rest & fluids` becomes advice in the
note and nothing in the chart — which is what #49 concluded.

## 6. Interchange: mock labs and pharmacies<a name="6-interchange"></a>

The point is to exercise the **round trip** — an order leaves, a result
returns and lands on the chart — with no real integration.

```
   hdh                          interchange/outbox            mock partner
   ─────                        ──────────────────            ────────────
   ServiceRequest(active)  ──▶  order bundle (FHIR)      ──▶  lab / pharmacy
                                                                   │
   LabResult / Prescription ◀── result bundle (FHIR)     ◀─────────┘
   + request.status=completed        inbox
```

- **Transport is a directory of FHIR Bundles**, not HTTP: deterministic,
  diffable, offline-testable, CI-friendly. The FHIR API module can serve
  the same bundles later for a live demo (§9 Q3).
- **The mock partner is a module**, not a script — a `PartnerAdapter`
  protocol with `LabPartner` and `PharmacyPartner` implementations, so a
  real integration replaces one without touching hdh.
- **Import is the interesting half.** A returning result must match a
  request. Matching by identifier is the happy path; anything that does
  not match — unknown request, duplicate result, result for a revoked
  order — goes to a **review queue**, never silently onto the chart. Same
  refuse-don't-guess contract, and exactly where a naive importer would
  quietly corrupt a chart.
- **Every import writes through `chartedit`** with `origin=EXTERNAL`, so
  `hdh chart history` shows it beside the pipeline and agent entries.

```bash
hdh orders list --mrn MRN…                    # what is outstanding
hdh orders release --visit 2064               # draft -> active, write outbox
hdh interchange run --partner mock-lab        # partner produces results
hdh interchange import --inbox …/inbox        # results -> chart, unmatched -> review
```

## 7. Where ontology coding lands<a name="7-coding"></a>

Only now do RxNorm and LOINC earn their place, and their shape is already
fixed by the `OntologyService` protocol that SNOMED and ICD-10-CM
implement:

- a **LOINC module** codes LAB requests and the results that return
- an **RxNorm module** codes MEDICATION requests, replacing the
  drug-name placeholder in `normalize.py`
- both plug into the same funnel contract, so `Tmax` and `B/P` resolve by
  term search rather than the hardcoded alias dict the vitals path uses
  today — the brittleness recorded in comprehension §12

Sequencing matters: coding a request is small and well-scoped once the
request exists; building the coders first leaves them nothing to attach
to.

## 8. Milestones<a name="8-milestones"></a>

| | Delivers | Proves |
|---|---|---|
| **A** | `ServiceRequest` + enums + OMOP-informed fields, migration, `LabResult.request_id`/`value_text`/`comparator`, `hdh orders list/release`, chartedit integration | requests are first-class, auditable, and analysable |
| **B** | Comprehension's fifth pass: plan-section orders become requests with verdicts; `rx_options` referral abuse retired | the note's plan finally reaches the chart |
| **C** | `hdh.modules.interchange`: `PartnerAdapter` protocol, mock lab + pharmacy, outbox/inbox bundles, `hdh interchange run/import`, unmatched-result review queue | the round trip closes without a real integration |
| **D** | LOINC module (labs), RxNorm module (medications) behind `OntologyService` | coding serves real requests; the vitals alias dict retires |

Each milestone is human-tested before the next begins.

## 9. Open questions — answer inline<a name="9-questions"></a>

1. **One table or two?** Proposal: one `ServiceRequest` with a `kind`
   discriminator, mapped to `MedicationRequest` or `ServiceRequest` at
   FHIR emit time. Alternative: mirror FHIR with two tables and duplicated
   lifecycle. One table, or mirror FHIR?

2. **Does a medication request supersede `Prescription`?** Proposal: no —
   request is the order, `Prescription` is what came back, linked by
   `fulfilled_by`. Alternative: fold them into one row with a status,
   simpler but loses "prescribed" vs "dispensed". Keep both, or merge?

3. **Transport.** Proposal: a directory of FHIR Bundles, with the FHIR API
   module optionally serving them later. Or should the mock partner be an
   HTTP service from the start, to exercise the API path?

4. **How real should the mock partner be?** Proposal: it generates
   clinically plausible results from the existing `LabSpec` reference
   ranges with a seeded RNG — a CBC comes back with plausible values and
   the occasional abnormal. Simpler alternative: echo fixed values.

5. **Follow-up as a request?** Proposal: emit a `FOLLOW_UP` request
   alongside `Visit.follow_up_days`, so the agent can amend it like any
   other order. Or leave it the scalar it is?

6. **How far to take OMOP (§3)?** Proposal: adopt the six field-level
   lessons now, defer `SPECIMEN` and `FACT_RELATIONSHIP` with the reasons
   recorded, and treat a full OMOP *export view* as a separate question
   for later. Or should an OMOP view be a milestone here, given
   `NOTE_NLP` already lines up with our mention model?

7. **Scope of milestone D.** Proposal: LOINC first (labs are the round
   trip we just built), RxNorm second. Both are loader+funnel modules on
   the scale of the SNOMED arc — do they need their own drill-down
   design, or is this doc enough?
