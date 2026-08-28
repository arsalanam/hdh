# Requests and read models

**Status:** proposed, 2026-08-28
**Applies to:** every `ServiceRequest` kind — lab, medication, referral,
procedure, follow-up
**Generalises:** `medication-orders-and-refills.md`, which arrived at this
principle for one kind before it was stated for all of them

---

## 1. The principle

> **A request is an intent. The chart records what happened.**
>
> Requests are transactional: placed at will, evaluated independently,
> fulfilled or not. **A read model is written only as the outcome of a
> fulfilment** — never by the request, and never directly.

A lab order is the model everyone already understands. Ordering an HbA1c
changes nothing about the patient. The result enters the chart when the lab
runs it and reports back, and the order closes at that moment. Nobody would
accept a system where placing the order wrote a value.

Every other request kind should work the same way, and none of them
currently does.

## 2. The evidence: the request layer was built and never wired

The schema has carried request-shaped columns since the service-requests
milestone. Measured on the development database, every one of them is empty:

| link | populated |
|---|---|
| `lab_results.request_id` | **0 of 8,309** |
| `prescriptions.request_id` | **0 of 2,175** |
| `service_requests.end_date` | **0 of 1,705** |
| `procedures` → request | *no column at all* |
| `visits` → request | *no column at all* |
| referrals | *no table at all* |

Statuses tell the same story: 13 DRAFT, 1,692 ACTIVE, **0 COMPLETED**.
Nothing has ever been fulfilled, because nothing fulfils.

Alongside them, `medication_statements` holds 1,553 rows that no analytical
module reads — a read model that exists, is populated, and is bypassed by
every consumer in favour of walking visits.

This is not five separate oversights. It is one missing idea: **the
generator writes chart rows directly**, so the request layer has never been
on the path between an intention and a fact.

## 3. Three layers, for every kind

```
REQUEST            ServiceRequest        intent — cheap, revocable, evaluable
    │                                    changes nothing about the patient
    │  fulfilled by
    ▼
FULFILMENT         evidence it happened  carries the outcome
    │                                    closes the request (end_date, §Q2)
    │  writes
    ▼
READ MODEL         the chart             what is true of the patient
```

**Sometimes the fulfilment *is* the read-model row.** A `LabResult` is both
the evidence and the chart entry. A `MedicationDispense` is evidence only —
the chart view is `MedicationStatement`, which accumulates across many
dispenses. The distinction is whether the chart entity spans more than one
event.

### 3.1 Per kind

| kind | request | fulfilment | read model |
|---|---|---|---|
| **LAB** | `ServiceRequest(LAB)` | interchange import | `LabResult` *(is the evidence)* |
| **MEDICATION** | `ServiceRequest(MEDICATION)` | `MedicationDispense` *(new)* | `MedicationStatement` |
| **PROCEDURE** | `ServiceRequest(PROCEDURE)` | procedure performed | `Procedure` *(is the evidence)* |
| **FOLLOW_UP** | `ServiceRequest(FOLLOW_UP)` | the return visit happening | `Visit` *(is the evidence)* |
| **REFERRAL** | `ServiceRequest(REFERRAL)` | acceptance or reply | — *(§7 Q2)* |

Four of the five need only a nullable `request_id` on a table that already
exists. Medication needs the dispense entity. Referral needs a decision.

## 4. What this buys, and it is mostly safety

**The agent places requests. The agent does not write the chart.**

That is the sentence worth having. Chart amendment is the most powerful
thing this system does and the most dangerous, and the current framing —
"the agent can amend the chart" — puts no structure around it. Under this
principle:

- an agent request is **cheap**: it changes nothing, so a wrong one is
  reviewable rather than damaging
- it is **evaluable**: `is_open()`, `can_refill()` and their refusals run
  against it before anything happens
- the chart changes only on **evidence of service**, and that evidence
  carries its own `origin`, so an agent-initiated fact is distinguishable
  from a clinician-initiated one for as long as the row exists

It also makes the demonstration honest. "AI-fronted mini EHR" is a stronger
claim when the AI cannot write a result — only ask for one.

## 5. What changes

**Additive, per kind:**

- `procedures.request_id`, `visits.request_id` — nullable, `NULL` meaning
  "generated before requests existed", exactly as `lab_results.request_id`
  is already documented
- `MedicationDispense` — the one new entity
  (`medication-orders-and-refills.md` §4.2)
- fulfilment stamps `end_date` and sets `status = COMPLETED` together; the
  importer currently does one and not the other

**Behavioural, and larger:**

- the generator emits **request → fulfilment → read model** rather than
  writing chart rows directly. This is the change that makes the layer real
  and the only one with a cost worth discussing (§6).
- read-model writes move behind a fulfilment function per kind, so "who
  wrote this row" has one answer

**Not proposed:** removing the ability to write a chart row without a
request. Historical data has none, external imports may have none, and a
system that cannot represent "this happened and nobody ordered it" is
describing a tidier world than the real one. `request_id IS NULL` stays
legal and means exactly that.

## 6. The cost, named

The generator change alters what every seed produces. #116 has just shown
what that costs: a generator change makes the previous eval baseline
**incomparable rather than merely stale**, because the cohort becomes
different patients. That is survivable once. It is a reason to do this
before the care-plan work resumes rather than during it, and a reason to do
all five kinds in one pass rather than five.

## 7. Open questions

**Q1. Does the generator move to requests in one milestone or per kind?**
Per kind is smaller and testable; one pass means one re-baseline instead of
five. Suggest **one pass**, given §6.

**Q2. What fulfils a referral?** A referral is answered by a letter, an
appointment, or a decline — none of which the chart models. Options: leave
`REFERRAL` unfulfillable for now and let the request stand alone; or add a
minimal outcome row. Suggest the former until there is something to
demonstrate.

**Q3. Does `Visit` really fulfil a `FOLLOW_UP` request?** It is the closest
thing to evidence that the follow-up happened, but visits also occur without
being asked for. A nullable link says both.

**Q4. Should read-model writes be *enforced* to come from fulfilment, or
merely conventional?** A guard is possible — a helper each writer must go
through. Convention is cheaper and weaker. Given the agent is one of the
writers, suggest a guard for the agent path at minimum.

**Q5. Does this subsume `Prescription`?** Under this principle
`Prescription` is an encounter artefact that is neither request nor
fulfilment nor read model. It may simply be a denormalised convenience that
retires once orders are real — but it has 2,175 rows and eight readers, and
that is a separate argument to have after the layer is working.

## 8. What this design does not have evidence for

- **No measurement that routing through requests changes plan quality.**
  It should be neutral; the eval harness can say so, and should be run
  either side of §5's behavioural change.
- **No estimate of the generator rework.** The prescribing path alone was
  larger than it looked in #116, and this touches five kinds.
- **The claim that four kinds need only a nullable column is unverified**
  beyond reading the schema. `Procedure` and `Visit` in particular may have
  writers that would need to learn about fulfilment.
- **No clinician input on what "fulfilled" means** for a referral, which is
  why §7 Q2 defers rather than guesses.
