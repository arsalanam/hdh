# Care planning, by talking to the agent

Every command below was run against a database built from scratch by
following this page, in this order. Timings and outputs are from that run,
not from what the code looks like it should do.

The whole thing takes about ten minutes, most of which is waiting for
`hdh generate`.

---

## What you need

| | why |
|---|---|
| Docker | PostgreSQL with `pgvector` — the bundled image ships it |
| An Anthropic API key | the agent, and the model that drafts each stage |
| AWS credentials with Bedrock access | **only** for semantic retrieval; skip it and use `lexical` |

Care planning **requires PostgreSQL**. It refuses on SQLite rather than
degrading, because retrieval over the clinical corpus is the whole
mechanism — a plan built without it would cite nothing.

---

## 1 · Install

```bash
uv sync --extra all          # or: pip install -e '.[all]'
```

`all` includes the `bedrock` extra (`boto3`, `pgvector`). Without it you can
still run care planning on `lexical` retrieval.

Put your keys in `.env` — `just` loads it into every recipe:

```bash
ANTHROPIC_API_KEY=sk-ant-...
AWS_PROFILE=default                 # only for vector retrieval
AWS_DEFAULT_REGION=us-east-1
```

> `uv run` does **not** read `.env`. Either go through `just`, or export the
> variables yourself first — a missing `ANTHROPIC_API_KEY` fails at the
> first model call, several steps after the one that was actually wrong.

---

## 2 · Start PostgreSQL

```bash
just deps
export HDH_DB_URL="postgresql+psycopg://hdh:hdh@localhost:5433/hdh"
```

---

## 3 · Build a chart, then prepare the database

```bash
hdh generate --patients 30 --years 3   # creates the tables as it goes
alembic stamp head                     # Alembic owns the schema from here
hdh db-init                            # extensions the modules need
```

`hdh db-init` prints what it did:

```
  installed  pg_trgm
  installed  vector
  installed  knowledge_chunks.embedding

  Database ready.
```

It is idempotent, and it does **not** fail on a server without pgvector —
it names the feature that will be unavailable and moves on.

> **Why not `alembic upgrade head`?** On an empty database it fails at
> migration 0002, which alters an enum that `create_all` creates and no
> migration does. Migrations are for databases that already exist; a fresh
> one is generated, stamped, then initialised. `hdh db-init` exists because
> extensions had nowhere else to live on that path.

---

## 4 · Load the clinical corpus

```bash
export HDH_CAREPLAN_RETRIEVER=vector+rerank    # or: lexical
hdh careplan ingest
```

```
  condition_guidelines       28 chunks
  med_safety                 10 chunks
  📚 ingested 38 chunks across 2 corpus/corpora
```

Ingestion goes through the same retriever you will search with, so the
embeddings are written now. Change `HDH_CAREPLAN_RETRIEVER` later and you
must re-ingest — a corpus loaded by one store and searched by another
returns nothing while looking fine.

Check it retrieves before spending anything on a plan:

```bash
hdh careplan search "elderly patient on glipizide" --corpus med_safety
```

---

## 5 · Pick a patient worth planning for

```bash
hdh careplan stratify --mrn MRN63193008
```

Multimorbidity is what makes a plan interesting. Anything with six or more
chronic problems will exercise triage, the rubric and the review loop.

---

## 6 · Ask the agent

```bash
hdh agent "build a care plan for MRN63193008"
```

The trace shows the pipeline routing it:

```
├─ guardrails     topic allowed ✓ (Care gaps planning)
├─ intent         care_plan · entities: MRN63193008
├─ tool-executor  tools exposed: get_patient_chart, get_care_gaps,
                  query_database, start_care_plan, show_care_plan,
                  approve_care_plan_stage, amend_care_plan_stage,
                  reject_care_plan_stage, show_care_plan_rubric,
                  write_care_plan_page
├─ validator      VALID ✓ — response is grounded in tool evidence
└─ streaming validated response (13,624 in / 1,833 out tokens)
```

**86 seconds**, and the answer stops after the first stage:

```
## Care Plan — David Fowler (MRN63193008), 84 M
Status: Stage 1 Complete — 7 Health Concerns Identified.
Awaiting your direction to proceed to Goals.

| # | Concern | Source |
| 1 | Polypharmacy Review — 6 active medications across multiple
      classes                              | med_safety/duplicate-class-therapy |
| 3 | TIA/Stroke History (Uncontrolled) — confirm antithrombotic
      appropriateness to event mechanism  | condition_guidelines/stroke-history |
...

⚠️ Deferred: Essential Hypertension, CAD without Angina, Chronic
   Diastolic Heart Failure — controlled, so triage set them aside.
   Recommend considering HTN given its role in stroke recurrence risk.

Next — your decision: approve all 7 · amend · expand · reject
```

Three things to notice, because they are the point of the module:

- **Every concern cites a document.** The plan is assembled from retrieved
  guidance, not composed by the model from the chart.
- **What was withheld is shown.** Triage deferred three controlled problems;
  a reviewer who cannot see that is reviewing a filtered list without
  knowing it.
- **It stopped and asked.** The graph pauses after each stage the model
  judged, and the agent does not approve on your behalf.

---

## 7 · Steer it

Interactive mode is the natural home for the review loop:

```bash
hdh agent
```

```
you> build a care plan for MRN63193008
agent> [7 concerns, paused] …

you> drop 6 and 7, and add hypertension
agent> kept 5 of 7 concerns … paused after goals

you> the goals are too vague — none has a number in it
agent> [concerns re-run with that feedback] …

you> approve
agent> paused after interventions …

you> write it up
agent> wrote care-plan-mrn63193008.html — 0 elements cite nothing
```

| you say | tool | what happens |
|---|---|---|
| approve / go on | `approve_care_plan_stage` | runs the next stage |
| keep 1,2,4 | `amend_care_plan_stage` | drops the rest, continues |
| that's wrong because… | `reject_care_plan_stage` | re-runs the stage with your reason |
| what does it score on? | `show_care_plan_rubric` | dimensions and anchors, read-only |
| write it up | `write_care_plan_page` | a self-contained HTML page |

Rejecting is cheap **because the later stages have not been built yet** —
the graph stopped before them. That is why review happens between stages
rather than at the end: a bad concern cannot silently shape every goal
underneath it.

---

## 8 · Refills, the same way

```
you> can MRN63193008 get another statin?
agent> Atorvastatin 20mg (order #1) may be refilled — 2 refill(s) remaining
you> go ahead
agent> refill recorded … 1 refill(s) remaining after this fill.
```

The agent does not decide. `can_refill` does — open order, not expired,
refills remaining — and every refusal names its own cause: *closed on
2026-03-01*, *expired*, *no refills remaining (3 of 3 issued)*. A fill is
stamped `origin=agent` for as long as the row exists.

---

## What it costs

From the run above, one patient with nine chronic problems:

| | |
|---|---|
| wall clock | 86 s |
| Anthropic | 13,624 in / 1,833 out |
| Bedrock | ~30 embed + ~30 rerank calls |

Most of the wall clock is retrieval: each topic queries three corpora, and
each query is an embedding plus a rerank round trip.

---

## When it does not work

| symptom | cause |
|---|---|
| `Could not resolve authentication method` | `uv run` does not read `.env` — go through `just` or export the keys |
| `semantic retrieval needs the pgvector extension` | run `hdh db-init` |
| `trigram retrieval needs the pg_trgm extension` | same |
| retrieval returns nothing for everything | corpus ingested under a different retriever — re-run `hdh careplan ingest` |
| the agent writes a plan itself instead of using the tools | it is not on this build; the system prompt forbids it (#142) |
| `no patient MRN…` | the cohort lives in its own database — check `HDH_DB_URL` |

---

## What this does not claim

The plan is **not** clinically validated, and a plan for a complex
multimorbid patient is not expected to pass its own rubric without editing.
The rubric is a regression detector and a way to direct a reviewer's
attention — not a gate that says a plan is safe. Everything here runs over
synthetic patients, and the published page says so on its face.
