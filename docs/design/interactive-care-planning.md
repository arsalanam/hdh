# Interactive care planning

Care-plan generation, driven and steered through the HdH agent: one
patient, one conversation, a clinician reading the plan and changing it
mid-flight — and then the rubric and the prompts adjusted from what was
seen rather than from what was guessed.

This is stage 4 and stage 5 of `careplan-state-and-graph.md`. That document
built the machinery (a `StateGraph`, a durable checkpointer, re-entry at any
node) and stopped before anything used it. Nothing in the codebase calls
`interrupt` today.

## 1. Why now, and why this shape

The cohort re-baseline (#123) produced a result that cannot be acted on
from the numbers alone:

| dimension | mean | governed the verdict |
|---|---|---|
| traceability | 2.46 | 21 of 24 |
| goal_quality | **3.00** | 12 of 24 |
| feasibility_burden | 3.08 | 11 of 24 |
| guideline_concordance | 4.21 | 2 of 24 |
| safety | 4.58 | 0 of 24 |
| completeness | 4.79 | 0 of 24 |

`goal_quality` scored **exactly 3 in all 24 runs** — standard deviation
0.00, across eight charts spanning one problem to seven, two rubrics, and
three independent runs each. Every other dimension moved. Two dimensions
have never governed a verdict in 36 graded runs across both baselines.

Those are not findings a score can resolve. A flat 3 is either a grader
default, an anchor the plan format can only ever land on, or a real and
uniform mediocrity — and the three have opposite fixes. Telling them apart
requires reading plans, which today means `hdh careplan show <id>` printing
into a terminal after the fact.

So the argument for this work is not "interactivity is nice". It is that
**the measurement loop is currently blind at the point where it has to
decide something**, and 24 more graded runs will not unblind it.

## 2. What exists, and what is missing

Measured against the code, not the plan:

| piece | state |
|---|---|
| `StateGraph` over 6 nodes | built (`graph.py`) |
| durable Postgres checkpointer | built (`checkpoints.py`) |
| re-entry at a node (`resume_at`) | built, used only by the revise loop |
| `interrupt()` anywhere | **absent** |
| care-plan tools for the agent | **absent** |
| clinician-readable rendering | **absent** — `careplan show` prints refs |
| prompts as tunable data | **absent** — one f-string, `generate.py:240` |
| rubrics as tunable data | built (`rubrics/*.json`) |

Two of those are worth stating plainly. The graph was adopted *for* human
review and has never been paused. And the rubric is data while the prompt
that produces what the rubric grades is a Python string literal — so "adjust
the rubric and the prompts" is currently one config edit and one code edit,
which is the wrong asymmetry for a thing meant to be tuned.

## 3. Where the graph pauses

After every **model** node, and only those. `PIPELINE` already labels each
node `deterministic` or `model`, so the pause set is derived, not listed:

```python
interrupt_after = [s.name for s in specs if s.kind == "model"]
```

That gives three pauses — after `concerns`, `goals`, `interventions` — and
means a node added to `PIPELINE` gets its review point automatically.
Deterministic nodes are not paused on: `stratify`, `triage` and `reconcile`
have no judgement to review, and pausing on them would train the reviewer
to press enter.

**Static `interrupt_after`, not dynamic `interrupt()`.** The pause is a
property of the pipeline, known at compile time, and belongs with the other
declarations rather than inside node bodies. It also keeps the nodes usable
unattended: the same functions run in the eval harness with no interruption
configured, so review is a property of *how the graph was compiled*, not of
what the nodes do. The harness and the clinician must run the same code, or
the thing measured is not the thing shipped.

### 3.1 What a reviewer may do at a pause

Three verbs, and each is an existing mechanism rather than a new one:

- **approve** — resume; `invoke(None, config)`
- **edit** — `update_state` with the amended items, then resume. Dropping a
  concern is an edit, not a rejection.
- **reject with feedback** — `resume_at` the same node with the feedback
  attached, which already exists for the revise loop

A rejected stage re-runs; the stages after it were never computed, because
the graph had not reached them. This is the substantive gain over reviewing
a finished plan: **a bad concern cannot silently shape every goal beneath
it**, because the goals do not exist yet.

### 3.2 What the reviewer must be shown

Not the state dict. At each pause: the proposed items, what each one cites,
what was dropped by the model and why, and what triage deferred before the
model saw anything. A reviewer who cannot see what was *withheld* is
reviewing a filtered list and cannot know it.

## 4. The agent surface

A tool pack, `careplan/agent_tools.py`, exposing `build_careplan_tools`, and
registered the way every other pack is. The tools are thin: each one is a
call into the graph plus a rendering.

| tool | does |
|---|---|
| `start_care_plan(mrn)` | opens a thread, runs to the first pause |
| `show_plan_state(thread)` | what is decided, what it cites, what was deferred |
| `revise_stage(thread, ...)` | edit or reject the paused stage, with feedback |
| `continue_plan(thread)` | approve and run to the next pause |
| `grade_plan(thread)` | run the rubric; per-dimension scores and the governing one |
| `show_rubric(name)` | the dimensions and their anchors, read-only |
| `publish_plan(thread)` | the clinician view (§5) |

### 4.1 Two guards this must not break

**The agent does not write the chart.** #121 established that a fact enters
the chart only as the outcome of a fulfilment, and
`test_the_agent_cannot_create_chart_rows` pins it. A care plan is not a
chart fact — it is a plan about one — so these tools may create
`care_plan_records` and may not create conditions, labs, prescriptions or
dispenses. The existing test keeps passing unchanged; if it fails, this pack
has reached somewhere it should not.

**The agent does not edit the rubric.** `show_rubric` is read-only and there
is deliberately no `edit_rubric`. A system that can rewrite the standard it
is graded against has no grade. Rubric changes are human edits to
`rubrics/*.json`, reviewed like any other change — and then measured on the
cohort, where the noise floor is 0.207 and an unmeasured improvement is not
one.

## 5. The clinician view

Two renderings of the same plan, because they answer different questions.

**In the terminal**, for iteration: compact, hierarchical, citations inline.
Fast enough to run twenty times while tuning.

**As a published page**, for review: the plan laid out as a clinician reads
one — concern, the goals under it, the interventions under each, what every
element traces to, what was deferred and why, and the grade with the
governing dimension named. This is the artefact you put in front of someone
who is not in the session, and the one that makes `goal_quality` legible.

Both render from the same structure. The page adds layout, not content.

## 6. Prompts as data, and why versioning them is not optional

The instruction that produces concerns, goals and interventions moves out of
`llm_selector` and onto disk beside the rubrics, loaded through a registry
in the same shape as `rubric.py` and `retriever.py`.

That much is ordinary. The part that is not:

**A prompt set gets a version, and the version is stamped on every plan and
every baseline it produced.**

We already learned this the expensive way, twice in one week. A generator
change moved what seed 4242 produces while the cohort name and half the MRNs
stayed the same, and `compare` printed a delta across charts that were not
the same charts. The fix was a cohort version and a refusal
(`evalset/__init__.py`). Prompt tuning is the identical failure with a
different noun: the cohort will be unchanged, the MRNs identical, and the
scores will move because the *prompt* moved. Without a stamp, that is
indistinguishable from a real improvement — and it is the improvement
everyone will want to believe.

So `compare` must refuse across prompt-set versions exactly as it refuses
across cohort versions, and for exactly the same reason.

## 7. Milestones

Each is a PR, each is independently useful, and each ends green.

**S4a — the graph pauses.** `interrupt_after` derived from `NodeSpec.kind`,
the three review verbs over existing mechanisms, tests that a rejected stage
re-runs and the stages after it were never computed. No agent, no rendering.

**S4b — the agent drives it.** The tool pack, wired into the registry;
terminal rendering; both guards in §4.1 pinned by tests. At the end of this
one, a care plan can be built conversationally for one patient.

**S4c — the clinician view.** The published page, from the same structure as
the terminal rendering.

**S5a — prompts as data.** Externalised, registry-loaded, versioned, and the
version stamped on plan records and baselines. `compare` refuses across
prompt-set versions.

**S5b — the tuning loop.** Regenerate one patient after a rubric or prompt
change and diff the result; then the cohort as the arbiter, because one
patient cannot clear a 0.207 noise floor and must not be allowed to look
like it did.

Then, and only then, the question this whole document exists to answer:
**what is wrong with `goal_quality`.** It is deliberately not a milestone.
The point of the machinery is that the answer should be visible by S4c, and
a milestone claiming to fix it before anyone has read a plan would be the
same guessing this design is meant to end.

## 8. The cost, named

- **Three pauses per plan is a real interaction cost.** A clinician
  reviewing three stages per patient will not do it at volume. This is a
  development and tuning instrument, not a clinical workflow, and it should
  not grow into one without saying so.
- **Interactive runs are not comparable to harness runs.** A plan a human
  steered has no place in a baseline. Plans must record whether they were
  reviewed, and reviewed plans must be excluded from cohort measurement.
- **Prompt externalisation touches the generation path**, which every
  measurement depends on. It has to be behaviour-neutral on the cohort, and
  "behaviour-neutral" means measured, not asserted.
- **A published page is a rendering of synthetic data**, and must stay
  visibly synthetic. Nothing here should be mistakable for a real patient
  record.

## 9. What this design has no evidence for

- That step-through review will actually reveal what `goal_quality` is
  doing. It is the best available instrument, not a guarantee. If three
  plans read fine on goals and still score 3, the fault is in the grader or
  the anchors, and that is a different investigation.
- That `interrupt_after` composes cleanly with the revise loop's
  `resume_at`. Both manipulate the same thread; whether re-entry from a
  pause and re-entry from a grade interact correctly is unknown until S4a
  runs.
- That prompts-as-data is behaviour-neutral. Moving a string should change
  nothing. Every previous "should change nothing" in this module was worth
  measuring, and two of them were wrong.

## 10. What the machinery found (added 2026-08-30)

Written after using it, because the point of §1 was that the answer was not
available from the scores.

**`goal_quality` is pinned by a schema field, not by the wording.** The
first plan rendered by S4c gave the mechanism: `goals_with_target = 0`,
while the rubric's anchor 5 requires "something measurable attached".
`GOAL_SCHEMA` marks `target_value` optional and the instruction never asked
for one.

**Asking was not enough.** `measurable-goals@1` changed exactly one prompt —
the goals instruction — to request a specific measurable value. Tuned
against `default@1` on MRN06934949:

| | default@1 | measurable-goals@1 |
|---|---|---|
| goals | 11 | 10 |
| goals carrying a target | **0** | **0** |
| goal_quality | 3 | 3 |

The model omits an optional field rather than filling it. The cohort run was
not needed to learn this and was not spent: the mechanism check —
*did any goal get a target* — is cheaper and more decisive than the score,
and it answers in two plans rather than twenty-four.

**What this implies for the next attempt.** `target_value` has to be
*required* in the schema, with an empty string permitted. Required-key with
empty-allowed is "you must decide", not "you must invent" — which is the
distinction the instruction already makes, and the one that keeps a
fabricated target from scoring well.

That change lives in the schema rather than the text, so to stay
attributable it has to become part of the prompt set: **what the model is
asked includes the shape it must answer in.** A schema change applied
globally could not be compared against the set it was meant to test, which
is the same attribution problem §6 exists to solve.

**And a second question this run did not answer.** Traceability scored 2
with *zero* uncited elements, in both runs. It is not failing on missing
citations but on citations that do not support the claim attached to them.
Nothing in the current facts measures that, so the grader is reading it
directly — which is exactly the kind of judgement `facts` exist to take off
the model.
