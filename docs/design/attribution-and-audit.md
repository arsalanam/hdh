# Attribution — Who Changed This, and How Would You Know (Draft)

**Where it lives:** `hdh.core.chartedit` (the one sanctioned mutation path) ·
`chart_audit_events` (the one trail) · **Follows:**
[patient-chart-completeness.md](patient-chart-completeness.md), which defined
what the chart *is* — this defines what may change it

---

## 1. Why now

The chart milestone finished by naming nineteen tables as the patient chart
and proving each is reachable. That raised a question it did not answer:
**which of them can be changed, by whom, and where does that get written
down?**

For an agent-first EHR the question is sharper than for a conventional one.
Most rows here are written by something automated — a generator, a
comprehension pipeline, an agent acting on a clinician's instruction — and
"the system did it" is not an answer a clinician can act on. If a plan cites
a condition that turns out to be wrong, the useful question is *who put it
there, when, on what basis, and what else did they touch*.

---

## 2. What exists, and works

Worth stating plainly, because the gap is narrower than it looks and the
design is already right where it reaches.

**One trail, not several.** `chart_audit_events` carries `occurred_at`,
`actor_name`, `actor_source`, `provider_id`, `patient_id`, `entity`,
`row_id`, `action`, `reason`, `before`, `after`. Care plans write to the
same table as chart corrections, so one query answers *what happened to this
patient* rather than most of it.

**One sanctioned mutation path.** `chartedit` applies amendments and voids,
and writes the event in the same transaction — the audit is not a courtesy
the caller extends. `AuditAction` is `create | amend | void`; an approval is
stored as an amend and *presented* as "approve", derived from the status
transition, so the enum never had to be extended on a deployed PostgreSQL.

**Both surfaces can read it.** `hdh chart history --mrn` for a human,
`chart_history` for the agent. Sample output:

```
2026-09-04 07:04  create  CarePlan#14   by care-plan review (agent)
     reason: written from a reviewed plan
     {} → {'status': 'user_edited', 'concerns': 4, ...}
```

**Actors are typed.** `EditSource` is `cli | agent | pipeline`, and
`actor_name` says which agent or which pipeline.

---

## 3. What we found by measuring

### 3.1 Twelve of the nineteen chart tables cannot be changed accountably

`chartedit` knows seven entities. The chart has nineteen tables.

```
chart tables           : 19
with a sanctioned edit : 7   allergies, conditions, lab_results,
                             prescriptions, service_requests, visits, vitals
WITHOUT one            : 12  family_history, functional_status, immunizations,
                             medication_dispenses, medication_statements,
                             patient_addresses, patient_contacts,
                             patient_coverages, patient_identifiers,
                             patients, procedures, visit_notes
```

Two things stand out.

**`patients` is on that list.** A person's name, address, insurer or
registered GP cannot be corrected through any accountable path. Those are
exactly the fields a receptionist changes weekly.

**Everything the chart milestone added is on it.** Functional status,
identifiers, addresses, contacts, coverage, and the new columns on
procedures and immunisations shipped with no way to correct them. That is
the honest cost of building the chart before the mutation path, and it is
this document's first item rather than an accusation.

### 3.2 A medication fill is attributed on the row and absent from the trail

`refills.record_fill` writes a `MedicationDispense` carrying
`origin=agent`. It writes **no audit event** — `refills.py` mentions the
audit only in a docstring.

So the row knows an agent filled it and the trail does not. *"Everything an
agent did to this patient"* misses medication fills, which is close to the
worst single omission available: it is the action with the most direct
physical consequence.

This is a different failure from §3.1. The attribution exists; it is in the
wrong place, and only one query finds it.

### 3.3 Nothing prevents an unaudited write

The audit is a convention that some call sites observe. `session.add(...)`
anywhere writes a chart row with no event and no complaint — which is how
§3.1 and §3.2 arose, and how the next one will.

The generator does this by design and correctly: bulk synthetic creation is
not a change to a chart, it *is* the chart. `scripts/seed_chart_demo.py`
does it deliberately as a demo aid. Neither is the problem. The problem is
that nothing distinguishes those from an ordinary write that should have
been recorded.

---

## 4. The principle

> **A chart row may be created or changed by a path that records who did it,
> why, and what it was before — or by a bulk-construction path that says it
> is one. There is no third kind.**

Two corollaries worth stating because they are what the design turns on:

**Attribution belongs in the trail, not on the row.** A column like
`origin=agent` answers "who did this row" and cannot answer "what did this
actor do", "what happened to this patient", or "what changed between
Tuesday and now". `medication_dispenses.origin` should stay — it is useful
at the point of use — but it is not the record.

**"The system did it" is not attribution.** `actor_source` already
distinguishes cli, agent and pipeline. What is missing is that most chart
tables cannot record any actor at all.

---

## 5. Milestones

### A1 — every chart table has a sanctioned edit path

Extend `chartedit`'s entity registry to the twelve. Most are mechanical: an
entity spec naming the editable fields and the visibility rule for a void.

Two need a decision rather than a spec, and are called out in §6.

### A2 — a fill leaves a trail

`record_fill` writes a `create` event for the dispense, with the actor it
already knows. Small, and the highest-value single row in this document.

The same review over every other write that carries attribution on the row
and nothing in the trail.

### A3 — an unaudited write is hard rather than easy

The weakest useful version: a test that every chart table's writers are
either `chartedit`, a declared bulk-construction path, or explicitly
exempt — the shape of M6's gate, applied to mutation instead of membership.

A stronger version — a session hook that refuses an unaudited write outside
a construction context — is more invasive and may cost more than it buys.
Start with the gate.

### A4 — the trail answers the questions people ask

`chart_history` returns events for a patient, newest first. It cannot yet
answer *what has this agent done today*, *what changed since Friday*, or
*show me every void*. Those are filters, not new machinery.

---

## 6. Open questions

1. **Is `patients` one editable entity, or several?** A name correction, an
   address change and a registered-GP change are different acts with
   different reviewers. One `Patient` spec is simpler; separate specs for
   the person-record tables track reality better.
2. **What is `before`/`after` for a row in a child table?** Amending a
   `functional_status` level is straightforward. Adding a second
   `patient_contact` is a create, not an amend — but the *fact* that
   changed is "how to reach this person", which is neither row alone.
3. **Does the generator need to declare itself?** It writes millions of
   unaudited rows correctly. A1–A3 need a way to say so that does not
   become a hole anything can climb through.
4. **Should a void cascade for the person tables?** Voiding a Visit already
   cascades to what it owns. Nothing owns a `patient_address`, so probably
   not — but "probably" is why it is here.
