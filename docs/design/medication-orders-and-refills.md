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

## 4. Proposed model

No new tables. Three roles that already exist, finally connected.

```
ServiceRequest(kind=MEDICATION)          the AUTHORISATION
    status, origin, requester_id,        what is allowed, by whom,
    valid_until, refills_authorised      until when, how many times
        │
        │  request_id  (the FK that already exists)
        ▼
Prescription                             each FILL
    one row per issue                    what was actually handed over
        │
        ▼
MedicationStatement                      the CURRENT LIST
    ACTIVE | COMPLETED | STOPPED         what the patient is on
```

**Refills remaining becomes a count, not a stored number.**

```
remaining = refills_authorised − (fills − 1)      # the first issue is not a refill
```

A derived count cannot drift. A stored counter decremented in two places
eventually will.

### 4.1 What gets added

| where | field | why |
|---|---|---|
| `ServiceRequest` | `refills_authorised: int \| None` | the count belongs with the authorisation, next to `status` and the dates |
| `ServiceRequest` | `valid_until: date \| None` | scripts expire; without this a 2022 order is refillable forever |
| `Prescription` | `dispensed_date: date \| None` | a refill has a date and **no visit** |

`ServiceRequest.end_date` already exists and may be able to serve as
`valid_until` — §7 Q2.

### 4.2 The constraint that has to move

**`Prescription.visit_id` is `NOT NULL`.** A refill does not happen at a
visit, so either the column becomes nullable or fills need their own table.

Nullable is the truer statement: plenty of real prescribing events have no
encounter behind them, and a synthetic visit invented to hold a refill is a
lie in the chart that every downstream count would then believe. It is also
additive rather than a new entity.

### 4.3 The decision, and the refusal

`can_refill(order, as_of) -> Decision` — deterministic, no model involved:

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str          # always populated, including when allowed
    remaining: int | None
```

Each refusal names its own cause: *revoked*, *expired on 2026-03-01*,
*no refills remaining (3 of 3 used)*, *no authorising order on record*.
A refill refused without a reason is indistinguishable from a system that
did not work, which is exactly the failure the care-plan module spends its
effort avoiding.

The **agent does not decide** whether a refill is allowed. It asks, and it
records the outcome. The check is arithmetic over the chart and belongs in
code for the same reason `stratify` does: it can be re-derived tomorrow and
argued with.

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

## 7. Open questions

**Q1. Is `Prescription` the fill event, or does it gain a sibling?**
Proposed: it is the fill. `service-requests-and-interchange.md` §4 already
calls it "the dispense/administration record", so this follows that reading
rather than introducing a competing one. The cost is that every existing row
becomes a fill with no order, which is honest — they were generated before
orders existed — and matches how `request_id IS NULL` is already documented.

**Q2. Does `ServiceRequest.end_date` serve as the validity date, or does
`valid_until` join it?** `end_date` currently means when the *therapy* is
expected to end. A script's validity is a different date and usually
shorter. Suggest a separate field, but this is a real call about whether the
distinction earns a column.

**Q3. Are refill *requests* modelled, or only outcomes?** A real EHR has a
queue: pharmacy asks, clinician approves. Modelling the queue means a
request row with its own status. Not modelling it means the agent simply
acts and the fill records what happened. Suggest **outcomes only** for now —
the queue is workflow, and the brief was realism without the workflow.

**Q4. When do monitoring gates arrive?** They need a drug→required-test
link. `caregaps` and the LOINC module already hold the pieces. Worth its own
milestone once refills exist to gate.

**Q5. Should the generator start issuing orders?** Today it writes
prescriptions directly. If it emitted a `MEDICATION` order per new therapy
and a fill per issue, the synthetic chart would exercise the whole path —
and the eval cohort would change again. Suggest yes, but in a later
milestone, measured.

## 8. Milestones

| | delivers | proves |
|---|---|---|
| **A** | `refills_authorised`, `valid_until`, `dispensed_date`; `visit_id` nullable; migration | the shape exists, nothing behaves differently |
| **B** | `can_refill()` and its refusals, with tests | the question is answerable and every "no" says why |
| **C** | agent tool: request a refill, record the fill or the refusal | `origin=AGENT` on a medication, end to end |
| **D** | generator emits orders and fills (Q5) | the synthetic chart exercises the path it describes |

A and B are offline and cheap. C is the demonstration. D changes the cohort
and needs a re-baseline, so it goes last.

## 9. Evidence this design does not have

- No measurement of how often a refill decision would be *wrong* under the
  proposed rules, because there is nothing to measure against yet.
- No clinician review of what refusal reasons should say. #95 is the channel.
- The claim that `visit_id` can be nullable without breaking downstream
  counts is **unverified, and the blast radius is measured**: eight modules
  reach prescriptions by walking `visit.prescriptions` —

      core/exporters.py        core/fhir/emitters.py    core/models.py
      core/notes.py            modules/billing          careplan/context.py
      comprehension/applier    comprehension/evaluate

  A visit-less fill is invisible to every one of them. Milestone A's real
  work is not the migration; it is deciding whether those become
  patient-level queries or whether fills stay attached to a visit after all.
  That question is worth settling before the column moves.
