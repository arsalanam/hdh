# Medications: the order, the fill, and the refill

**Status:** proposed, 2026-08-28
**Extends:** `service-requests-and-interchange.md` §4, which specified the
order↔prescription link that was never wired
**Prompted by:** the agent can amend the chart through orders, and a refill
is the commonest medication decision in primary care. We cannot currently
answer *"can this be refilled?"* at all.

---

## 1. The gap, measured

`service-requests-and-interchange.md` §4 already drew this:

> A medication `ServiceRequest` is the **order**; a `Prescription` is what
> came back. They gain a link, not a merge.

The link exists as a column. On the development database:

| | count |
|---|---|
| `FOLLOW_UP` requests | 1,692 |
| `MEDICATION` requests | **4** |
| prescriptions | 2,175 |
| **prescriptions with `request_id` set** | **0** |

So the generator writes prescriptions with no authorising order, and
comprehension writes the occasional medication order that never becomes a
prescription. Two layers, both real, that have never met.

This is not a new design. It is an unfinished one, and the refill question
is what makes finishing it worth doing.

## 2. What a refill actually asks

*"Can Mrs Ahmed have another three months of atorvastatin?"* decomposes into
four questions, and we can answer none of them:

| question | needs | have |
|---|---|---|
| Is there an authorisation? | a `MEDICATION` order the script points at | link never populated |
| Is it still valid? | an expiry on the authorisation | **nothing anywhere** |
| Are there refills left? | authorised count *minus fills taken* | authorised only; **no fill events at all** |
| Is anything outstanding? | monitoring due, review date | out of scope — §6 |

The third is the sharpest. `Prescription.refills` records how many were
authorised and never changes, because nothing records that one was used.
A number that only ever counts down in the real world and never moves here
is worse than no number: it reads as current and is not.

## 3. What is not recorded

Beyond the three above:

- **prescriber on `Prescription`** — `ServiceRequest.requester_id` exists;
  the script itself has none, so "who authorised this" is answerable only
  for orders
- **therapy status and dates on `Prescription`** — `MedicationStatement`
  has `ACTIVE/COMPLETED/STOPPED` with start and end; the script has only
  `voided_at`, which means *entered in error*, not *stopped*
- **stop reason** — so "stopped for myalgia" cannot be said (see #115)
- **route, sig, quantity on the script** — `ServiceRequest` carries all
  three; `Prescription` carries none
- **pharmacy or destination** — absent, and deliberately stays absent (§6)

## 4. Proposed model — revised 2026-08-28

The first version of this section optimised for **"no new tables"** and let
that push it into making `Prescription.visit_id` nullable. That was the
wrong trade, and the reason is visible in the schema:

> **`prescriptions` has no `patient_id`.** It is reachable only through a
> visit. A nullable `visit_id` would not merely be loose — it would create
> rows belonging to nobody, reachable from nothing.

The deeper fault was conflating **the requesting mechanism** with **the
medications a patient ever had**. They are different questions with
different anchors, and the schema already knows it:

| table | anchor | rows | what it is |
|---|---|---|---|
| `prescriptions` | visit **NOT NULL**, no `patient_id` | 2,175 | a line written **at an encounter** |
| `medication_statements` | `patient_id`, **no `visit_id` at all** | 1,553 | what the patient **is / was on** |
| `service_requests` | `patient_id`, `visit_id` **nullable** | 1,705 | the **requesting** layer |

Two of the three sides are already right and already populated. Only one
thing is genuinely missing, and it is not a column — it is **supply**.

### 4.1 Four roles, each keeping the constraint that is true for it

```
ServiceRequest(kind=MEDICATION)        REQUESTING — the authorisation
  patient_id, visit_id nullable        what is allowed, by whom, until when
  + refills_authorised, valid_until    end_date = closed (§7 Q2)
        │
        │  authorises
        ▼
MedicationDispense            (new)    SUPPLY — each fill
  patient_id, request_id               a date, a quantity, NO visit needed
  dispensed_date, quantity
        │
        │  updates
        ▼
MedicationStatement                    HAVING — what the patient is on
  patient_id, no visit_id              ACTIVE | COMPLETED | STOPPED
  status, start_date, end_date         already visit-free, already populated

Prescription                           ENCOUNTER — unchanged
  visit_id NOT NULL                    what was written at this visit
```

**`Prescription` is left exactly alone.** Its `NOT NULL` is correct and
says something true: this is a line on an encounter. It is not the
medication history, and it was never asked to be until this document asked
it to.

### 4.2 The one new table

```python
class MedicationDispense:
    patient_id: int                    # anchored to the person, not the visit
    request_id: int | None             # the authorisation it draws on
    drug_name: str
    dispensed_date: date
    quantity: float | None
    days_supply: int | None
    origin: RequestOrigin              # GENERATED | AGENT | CLINICIAN | EXTERNAL
    visit_id: int | None               # usually null; set when it happened at one
```

One row per supply event. A refill is simply the second one.

`origin` carries the same enum the requests use, so an agent-issued fill is
attributable and distinguishable from a clinician's without a second
mechanism.

### 4.3 Refills become arithmetic

```
issued    = count(dispenses for this order)
remaining = refills_authorised − (issued − 1)      # the first issue is not a refill
```

Derived, not stored. A counter decremented in two places drifts, and
`Prescription.refills` already demonstrates the failure — it records what
was authorised, never moves, and therefore reads as current when it is not.

### 4.4 The decision, and the refusal

`can_refill(order, as_of) -> Decision`, deterministic, no model involved:

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str          # always populated, including when allowed
    remaining: int | None
```

It composes with the §7.1 `is_open()` test rather than restating it: an
order that is closed, revoked or expired fails before refills are counted.
Each refusal names its own cause — *closed on 2026-03-01*, *expired*,
*no refills remaining (3 of 3 issued)*, *no authorising order on record*.

The **agent does not decide** whether a refill is allowed. It asks, and it
records the outcome. The check is arithmetic over the chart and belongs in
code for the same reason `stratify` does: it can be re-derived tomorrow and
argued with.

### 4.5 What this costs

One table and two columns, against the previous version's zero tables and a
weakened constraint. The extra table is the cheaper of the two — a new
entity is additive and inspectable, whereas a nullable `visit_id` would have
been invisible to the eight modules that reach prescriptions through visits
(§9) and would have left orphan rows in the most-read table in the chart.

## 5. What this demonstrates

This is the smallest thing that makes "AI-fronted mini EHR" a claim rather
than a slogan. A clinician says *"can she have another three months of the
atorvastatin?"* and the agent answers from the chart — yes, two refills
left, valid until March; or no, the authorisation expired in January and
this needs a new prescription. Both answers cite rows.

It also exercises the `origin=AGENT` path that exists and is untested at the
medication kind: an agent-issued fill is attributable, auditable through
`chartedit`, and distinguishable from a clinician's.

## 6. What this deliberately does not build

Named because the boundary is the design decision:

- **No pharmacy, no dispense routing, no stock.** A fill records that one
  happened, not where it went.
- **No controlled-drug rules.** Real ones are jurisdictional; encoding a
  plausible fiction would be worse than encoding nothing.
- **No monitoring gates.** "A statin refill needs LFTs in the last year" is
  the most attractive next step and needs a drug→monitoring link we do not
  have. Deferred, not rejected — §7 Q4.
- **No partial fills or quantity reconciliation.** One fill, one row.
- **No refill *request* queue** unless §7 Q3 says otherwise.

## 7. Open questions — answered 2026-08-28

Kept as questions with their decisions attached, so the reasoning that was
open stays next to what closed it.

**Q1. Is `Prescription` the fill event, or does it gain a sibling?**

> **Decided: it is the fill.** Follows
> `service-requests-and-interchange.md` §4, which already calls it "the
> dispense/administration record". Existing rows become fills with no
> order, which is honest — they were generated before orders existed.

**Q2. Does `ServiceRequest.end_date` serve as the validity date?**

> **Decided: no, and `end_date` means something stronger than this document
> assumed.** It is not the therapy end and not the script validity. It is
> the **end of the request's own life**:
>
> - a request with `end_date` set is **closed**
> - a closed request **cannot be acted upon**
> - **the chart may not be modified on the basis of one**
> - it is stamped **when the request is served**, by the interchange that
>   served it
>
> That makes `end_date` an authorisation boundary rather than a clinical
> date, and it is the more useful reading: the agent amends the chart
> *through* orders, so "may this order still be acted on" is a question
> asked before every amendment. `valid_until` is therefore a separate
> field — a script can expire while its order is still open, and an order
> can close while the script is still in date.
>
> **Two gaps this exposes.** The column is written nowhere: 1,705 requests,
> **0 with `end_date` set**. And `interchange.importer` sets
> `status = COMPLETED` on fulfilment without stamping when — so a served
> request is already indistinguishable from an open one by date, and will
> stay that way until the importer closes it properly.

**Q3. Are refill requests modelled, or only outcomes?**

> **Decided: outcomes only.** A queue is workflow, and the brief was
> realism without the workflow. The agent acts and the fill records what
> happened.

**Q4. When do monitoring gates arrive?**

> **Decided: later, own milestone.** They need a drug→required-test link
> that does not exist. Deferred, not rejected.

**Q5. Should the generator issue orders?**

> **Decided: yes, but last.** Milestone D. It changes the cohort and needs
> a re-baseline, and #116 has just demonstrated what that costs — a
> generator change makes the previous baseline incomparable rather than
> merely stale.

### 7.1 What Q2 adds to the model

An open/closed test, and a rule that uses it:

```python
def is_open(request, as_of) -> bool:
    """May this request still be acted upon?"""
    return (
        request.voided_at is None
        and request.end_date is None
        and request.status in {DRAFT, ACTIVE}
    )
```

Nothing may amend the chart on the basis of a request that fails this, and
the refusal names which clause failed. This is the same discipline as
`can_refill` in §4.3 and for the same reason: a refusal without a cause is
indistinguishable from a system that did not work.

It also gives the interchange something to do on fulfilment — stamp
`end_date`, not merely flip `status` — which closes the gap that today
leaves every served request looking open.

## 8. Milestones

| | delivers | proves |
|---|---|---|
| **A** | `MedicationDispense`; `refills_authorised` and `valid_until` on the order; `is_open()`; the importer stamps `end_date` on fulfilment; migration | the shape exists, served requests stop looking open, `Prescription` is untouched |
| **B** | `can_refill()` and its refusals, with tests | the question is answerable and every "no" says why |
| **C** | agent tool: request a refill, record the fill or the refusal | `origin=AGENT` on a medication, end to end |
| **D** | generator emits orders and fills (Q5) | the synthetic chart exercises the path it describes |

A and B are offline and cheap. C is the demonstration. D changes the cohort
and needs a re-baseline, so it goes last.

## 9. Evidence this design does not have

- No measurement of how often a refill decision would be *wrong* under the
  proposed rules, because there is nothing to measure against yet.
- No clinician review of what refusal reasons should say. #95 is the channel.
- **Which readers need to learn about dispenses is unmeasured.** Eight
  modules reach medications by walking `visit.prescriptions` —

      core/exporters.py        core/fhir/emitters.py    core/models.py
      core/notes.py            modules/billing          careplan/context.py
      comprehension/applier    comprehension/evaluate

  With `Prescription` left alone none of them *break*, which is the point of
  the revision. But a patient whose only recent supply was a refill now has
  a medication the encounter view cannot see, so each reader has to be
  asked whether it wants encounter lines or the medication list. Most
  probably want `MedicationStatement`, which is already visit-free and
  already populated — `careplan.context` in particular reconstructs a
  medication list by walking visits, which is the long way round to
  something that exists.

- **`MedicationStatement` is 1,553 rows and no consumer reads it.** If it
  is the right answer for those eight readers, that should be checked
  before a ninth is written against visits.
