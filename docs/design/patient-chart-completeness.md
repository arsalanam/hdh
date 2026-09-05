# The Patient Chart — What a Person Is, and Where a Module Stops

**Where it lives:** `hdh.core` — the chart is core by definition; this
document is largely about defending that boundary ·
**Supersedes nothing; constrains everything after it**

---

## 1. Why this, and why now

hdh is an agent-first mini-EHR. The agent answers from the chart, and the
chart is therefore the ceiling on what the agent can ever be right about.
Two questions that a family physician asks daily currently have no good
answer, and one of them has a *confidently wrong* one.

This document audits what the chart holds, states the rule that keeps the
chart from becoming a dumping ground as modules arrive, and defines the
milestone at which the basic chart is **complete** — meaning a module may
extend it, but nothing essential to a person is missing from it.

---

## 2. What we found, by running it

Measured against the working database, not read off the models.

### 2.1 The chart asserts a clinical fact from missing data

`patient_to_text` renders the allergy line as:

```python
"Allergies    : " + (", ".join(a.substance for a in patient.allergies) or "NKDA")
```

**36 of 60 sampled patients have no allergy rows, and every one of them
renders `NKDA`** — *no known drug allergies*. That is an assertion. The
schema cannot distinguish *never asked* from *asked, and none*, and the
exporter resolves the ambiguity toward the reassuring reading.

Everywhere else this project refuses exactly this move. Triage records what
it deferred so an absence can be told from an omission. A plan element
citing nothing is rendered as the loudest thing on the page. `snomed_tag`
exists so a code cannot imply a hierarchy it does not have. The allergy line
is the one place we do the opposite, and it is the place where being wrong
is most expensive.

**A blank allergy list is not a negative finding.** It has to be possible to
record that someone was asked.

### 2.2 The severity is stored and then dropped

```
stored:   substance='Aspirin'  reaction='GI upset'  severity=MILD
rendered: Allergies    : Aspirin
```

Anaphylaxis to penicillin and a mild rash render identically to the agent.
The data is right there in the row.

### 2.3 Three tables are generated and unreachable

`immunizations` and `procedures` appear **zero times** in `patient_to_text`.
`allergies`, `immunizations`, `procedures` and `family_history` are exposed
by **no intent** in `INTENT_TABLES`, so the SQL tool cannot see them either.

So an allergy question routes to `patient_lookup`, the agent cannot query
the table, and the chart summary tells it `NKDA`. The failure is not that
the agent lacks data — the data is generated and sitting in Postgres. It is
that nothing connects it to a question.

### 2.4 There is no functional status at all

No table describes what a person can physically do. Not mobility, not
sensory impairment, not activities of daily living, not aids used, not
communication needs.

The care-plan rubric grades **`feasibility_burden` — "Could this patient
actually carry out this plan?"** — from `intervention_count`,
`burden_limit`, `burden_flagged` and `bare_goals`. Those are counts. A plan
of four interventions for someone who cannot leave the house scores the same
as four for someone who drives. We are grading a question the chart cannot
answer.

`SocialView` carries `lives_alone` (inferred from marital status, with the
inference recorded as `lives_alone_basis`), `smoker` and `marital_status`.
That is the whole of it.

### 2.5 The person is modelled as one flat row

`patients` has 25 columns, and every multiple is singular:

| what a person has | what the schema allows |
|---|---|
| names — legal, preferred, maiden, and the one they answer to | `first_name`, `last_name` |
| identifiers — MRN, national ID, member number | `mrn`, and `insurance_id` doubling as one |
| addresses — home, mailing, and where they lived last year | one flat set of columns |
| contacts — mobile, home, work, the daughter who answers | `phone`, `email` |
| coverage — primary, secondary, a policy holder who is not them | `insurance_name`, `insurance_id` |

Sex is administrative only; there is no gender identity, and no pronouns.
There is no recorded general practitioner or care team, which matters for a
system whose interventions carry an `owner_role`.

None of this is a modelling error for a synthetic single-practice dataset.
It becomes one the moment a second identifier, a second address, or a
secondary insurer has to be recorded — and "add another column" is the wrong
answer each time.

---

## 3. The boundary: what belongs in the chart

The rule this document exists to fix in place, because the next module will
test it:

> **The chart holds what is true of the person between encounters. A module
> holds what is true of one episode of care.**

An allergy is true of the person on a Tuesday when nothing is happening. A
ward, a bed, an admitting consultant and a discharge summary are true of one
admission. The first belongs in `hdh.core`; the second belongs in whatever
module owns inpatient care, and **almost none of it should reach the chart.**

Three tests, applied in order:

1. **Does it outlive the episode?** A wheelchair does. A bed number does not.
2. **Would a different service need it to be safe?** A penicillin allergy,
   yes — anyone prescribing anything. A theatre list position, no.
3. **Is it about the person, or about our handling of them?** Deafness is
   about the person. A referral's triage priority is about our queue.

What an inpatient module would contribute back to the chart from an entire
admission is small and specific: a procedure performed, a new allergy
discovered, a medication started, a diagnosis confirmed, a change in
function. The admission itself — location, transfers, ward rounds, the
discharge process — stays in the module. That asymmetry is the point, and it
is what makes "complete" a reachable state for the chart rather than a
moving target.

**Modules extend, they do not relocate.** The schema registry already
enforces this shape: a module may add columns to a core entity and may
declare new entities of its own, but it may not rename or re-declare a core
table's columns. The care-plan module is the worked example — four new
tables of its own, plus nothing added to `patients`.

---

## 4. The milestone: a complete basic chart

**Complete** means: a family physician reading this chart is not missing
anything that would change what they do, and the agent can answer from it
without inferring. It does not mean exhaustive.

The work, ordered by how badly it currently misleads.

### M1 — Stop asserting what we do not know · **safety**

The `NKDA` line is a defect, not a gap, and it should not wait behind
schema work.

- Allergy status becomes recordable: *no known allergies* is an assertion
  with a date and a source, distinct from an empty list.
- `patient_to_text` renders "not recorded" when nothing was asked, and
  carries **reaction and severity** when something was.
- The same review over every other place an absence is rendered as a finding.

### M2 — Make what exists reachable

The data is generated and invisible. This is wiring, not modelling.

- `allergies`, `immunizations`, `procedures`, `family_history` join the
  chart text and the relevant `INTENT_TABLES`.
- Each gains a `semantics` block, per #93 — the gate added there will
  otherwise fail them, which is the gate doing its job.

### M3 — The person as a person

- Names beyond two fields: preferred name at minimum.
- Identifiers as rows, not columns — MRN, national ID, member number, each
  with a type and an issuer.
- Contacts and addresses as rows, with a use (home/mobile/work) and a period.
- Coverage as rows: primary and secondary, policy holder, group, period.
- Gender identity recorded separately from administrative sex; pronouns
  recorded rather than inferred. A system that writes to and about patients
  gets this wrong by default otherwise.
- Registered GP / care team.

### M4 — Functional status

The gap with the clearest downstream consumer: `feasibility_burden` is
currently graded without it.

- Mobility and aids used; sensory impairment (vision, hearing);
  communication and interpreter needs; ADL/IADL summary where recorded.
- Coded where a coding exists, and honestly blank where nothing was assessed
  — the same discipline as M1, or this becomes a second place that invents
  reassurance.
- `SocialView` gains it, and the care-plan rubric's feasibility dimension
  gets facts that bear on the question it asks.

### M5 — Clinical history worth the name

- **Surgical history** as distinct from in-visit procedures. `procedures`
  already permits a null `visit_id`, so the shape exists and nothing uses
  it — a past appendectomy has no visit in this practice.
- Procedures carry a code (CPT/SNOMED), body site and laterality.
- Immunizations carry status, and **refusal/contraindication** — a declined
  vaccine is a clinical fact and currently unrecordable.
- Allergies carry category, criticality, verification and clinical status,
  and a coded substance (RxNorm for drugs, SNOMED otherwise) rather than
  free text.

### M6 — The gate

So that "complete" survives contact with the next module.

- A test asserting every core chart table is reachable from at least one
  intent and appears in the chart export. M2 fixes today's four; the gate
  stops the fifth.
- The §3 rule written into the module authoring guide, with the inpatient
  example, so a module author reads it before deciding where a column goes.

---

## 5. What this deliberately does not do

- **No FHIR resource-per-concept rewrite.** The chart stays relational and
  exports as FHIR through the emitters that already exist. Modelling
  `Coverage` as rows is not the same as adopting the resource wholesale.
- **No clinical-validity claim.** The cohort stays synthetic, and a complete
  chart is a complete *synthetic* chart.
- **No inpatient module.** §3 uses it as the worked example precisely
  because it does not exist yet, and the boundary is easier to agree before
  someone has code that would rather cross it.

---

## 6. Decisions

The four questions §5 left open, answered 2026-09-04.

### 6.1 Each of these is a row, and absence means none

Allergies, family history, immunisation status — records, not columns on the
patient. The allergy record:

```
id · patient_id · substance · drug_code · code_standard
     severity · reaction · noted_date · last_happened · notes · voided_at
```

`drug_code` carries the coded substance and `code_standard` says which
vocabulary it belongs to, because a bare identifier cannot — and guessing
from its shape is how a drug allergy becomes a food one. `last_happened` is
separate from `noted_date`: when it was written down and when it last
actually happened are different questions, and a severe reaction thirty
years ago is weighed differently from one last month.

**A patient with no allergy rows has no known allergies.** That is the
chart's contract rather than an inference drawn at render time, and it holds
because every chart here is generated or written through the chart tools,
both of which record an allergy when there is one.

*What this costs, recorded so it is a decision and not a surprise:* "never
asked" is not representable. That is correct for a generated chart and
becomes wrong the day charts are imported from a source that may simply not
have asked — at which point this needs an explicit *asked, and none*
assertion. The trigger is data import, not a date.

What §2.1 identified is still fixed, because the defect was never the
contract: the line asserted `NKDA` in the vocabulary of a clinician who had
asked. It now reads `none recorded (NKDA)`, and a populated list carries the
severity and reaction that were being dropped.

### 6.2 The chart is current state; in-flight work carries its own status

The principle, which reaches further than functional status:

> Everything in the chart is **current**. Things that are in flight — a
> fulfilment order, a referral request, a care plan — carry a status while
> they are active, and when they complete, **their outcome merges into the
> chart** and the tracking row has done its job.

This is the requests-and-read-models rule the repository already runs on —
*a request is intent, fulfilment is evidence, and the read model is written
only on fulfilment* — stated as a property of the chart rather than of one
subsystem. It is what makes "complete" a bounded target: the chart does not
grow a column every time a module invents a workflow, because the workflow
stays in the module and only its result lands here.

Functional status follows from it. Record what is true of the person now,
where a value was actually recorded, and only what changes a plan. An
assessment in progress is not chart data; its conclusion is.

### 6.3 Multiple migrations

M3–M5 land as separate migrations rather than one "person" change. Each is
independently useful and independently reversible, and 0018 and 0019 have
already shown that a small add-column migration against a real database is
where the surprises live.

### 6.4 The generator comes last, deliberately

Stub data while the milestones are built; the generator is updated once,
afterwards, and the next generation produces realistic synthetic data across
all of it.

This means M3–M5 will ship with tables that are correct and largely empty,
and the agent's answer will change from *wrong* to *absent*. That is an
improvement and it is not the milestone — the milestone is when a generated
chart populates them.

---

## 7. Where M1 and M2 landed

Both shipped together, since M2 is wiring rather than modelling.

**M1** — allergies carry severity and reaction to the page; a voided allergy
is excluded rather than shown; an empty list says it came from an empty list;
the record gains `drug_code`, `code_standard`, `last_happened` and `notes`
(migration 0019).

**M2** — `allergies`, `immunizations`, `procedures` and `family_history`
reach the chart text and the intents that need them. Prescribing can see
allergies; care gaps can see immunisations. Each declares its meaning under
#93, including the two an agent gets wrong by reasoning: that an empty
allergy list means none rather than unasked, and that a procedure with a
NULL `visit_id` is history recorded from elsewhere — so joining procedures
to visits silently drops exactly the surgical history someone asked for.

Verified end to end: asked whether a patient with a recorded aspirin allergy
could take an NSAID, the agent found the allergy and reasoned from its
severity — the field that was in the row and not on the page.

**M3 shipped** — identifiers, addresses, contacts and coverage are records;
`preferred_name`, `gender_identity`, `pronouns` and a registered provider are
columns on the person. Migrations 0020 (tables, with a backfill from the flat
columns) and 0021 (columns), separately as decided.

The flat columns remain the primary value while readers move over, which is a
real cost: `patients.phone` and the rank-1 contact are the same fact twice. A
test asserts they agree, so the duplication cannot drift silently, and
removing the flat columns is its own migration once nothing reads them.

Writing it turned up the **third incarnation** of this repository's oldest
bug. `patient.sex` is not reliably the `Sex` enum — a freshly built instance
carries `Sex.FEMALE`, one reloaded after a flush carries the raw column value
`'F'`, and both are `str` subclasses so neither raises. The FHIR emitter
tested `str(sex).endswith("FEMALE")`, which is right for `'Sex.FEMALE'` and
wrong for `'F'`, so **female patients were exported as male** on every path
that produced a raw string — three of eight in a sample. `patient_to_text`
called `sex.label`, which raises outright on `'F'`. Comparisons now live on
the enum as `Sex.coerce`, where `is_male` and `label` already were.

**M4–M6 remain open.**
