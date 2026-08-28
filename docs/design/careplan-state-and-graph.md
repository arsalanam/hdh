# Care plan: explicit state, declared nodes, and a real graph

**Status:** proposed, 2026-08-27
**Supersedes:** parts of `care-plan-module.md` §4, §7, §10
**Prompted by:** the module cannot become an interactive care-plan creator on
its present shape, and the reasons are structural rather than incidental.

---

## 1. What this is for

Phase 3 shipped a pipeline that generates a plan, grades it, and revises it
once or twice. It is batch software: one invocation in, one plan out.

The destination is different. An **interactive** care-plan creator means a
clinician saying *"why did you defer the heart failure?"*, *"add a falls-risk
concern"*, *"make the second goal measurable"* — and the system knowing what
"the second one" refers to, what has already been decided, and how to apply an
edit without regenerating everything around it.

That is not the current architecture with more features bolted on. It needs
three things the module does not have: **state that outlives a call**, **a
thread the conversation belongs to**, and **nodes that can be re-entered
rather than only re-run**.

## 2. What is actually true today

Stated as findings rather than opinion, because the fix depends on the
diagnosis being right.

**There is no message history anywhere.** Both LLM call sites —
`generate.py:215` and `evaluate.py:379` — build
`messages=[{"role": "user", "content": prompt}]`. One turn, no history, no
continuity. What flows between nodes is *data*: node 4's prompt contains node
3's concern text, but the model has no idea it wrote that concern.

**State is local variables.** `plan.py::generate_plan` holds `context`,
`flags`, `topics`, `deferred`, `draft`, `reconciliation` as Python locals. There
is no object representing "this plan run". Checkpointing is expensive precisely
because there is nothing to serialise — it would mean inventing a format for
eight locals.

**The node sequence is hardcoded in three places.** `plan.py` calls them in
order; `revise.py` does index arithmetic over exactly three of them
(`start == 0`, `start <= 1`, `start == 2`); `rubric.REVISABLE_NODES` lists their
names. Adding a node touches `generate.py`, `plan.py`, `revise.py`,
`rubric.py`, both rubric JSON files, `PlanDraft`, `assemble.py` and `facts.py`.

**`checkpoint_thread_id` exists and is unused.** The schema has carried a column
for LangGraph's checkpointer since milestone 1. Nothing writes it.

**There is no LangGraph.** The design names it in four places and
`langgraph>=1.2.10` is installed as part of the `agent` extra. The module
imports it zero times. That divergence was never raised, and it is the reason
Phase 4 now looks expensive.

## 3. Three kinds of memory, which are not the same thing

Conflating these produced a bad argument in an earlier discussion, so they are
separated here deliberately.

| | What it is | Needed? |
|---|---|---|
| **Clinician dialogue** | the conversation about *this plan*, at agent level | **Yes** — interactivity is impossible without it |
| **Node-internal turns** | a node that asks, gets a partial answer, asks again | **Should be possible**; nothing needs it yet |
| **Cross-node context** | piping node 3's raw turns into node 4 | **Unknown** — arguable, untested, and no evidence either way |

The first two are requirements. The third is an open question, and this design
takes no position on it beyond making it *possible* and *per-node* rather than
global.

## 4. Target architecture

### 4.1 One state object

Everything a plan run knows, in one serialisable place.

```python
class CarePlanState(TypedDict, total=False):
    # identity
    thread_id: str
    patient_mrn: str

    # node 1-2b, deterministic
    context: CarePlanContext
    flags: list[RiskFlag]
    topics: list[Topic]
    deferred: list[str]

    # nodes 3-6
    concerns: list[ConcernDraft]
    goals: list[GoalDraft]
    interventions: list[InterventionDraft]
    reconciliation: ReconcileReport | None
    dropped: list[str]

    # §9 and M3c
    rubric_id: str
    evaluation: Evaluation | None
    rounds: list[Round]

    # node 7
    plan_id: int | None

    # interaction
    messages: Annotated[list, add_messages]   # clinician dialogue
    pending: str                              # what the graph is waiting for
    edits: list[Edit]                         # human changes, with provenance
```

Serialisability is the real constraint and the real work. Every value above must
round-trip through JSON, which the frozen dataclasses do not do today. This is
the same cost under any option, framework or not.

### 4.2 Nodes become uniform and declared

```python
Node = Callable[[CarePlanState], Mapping[str, Any]]   # returns a PARTIAL state

@dataclass(frozen=True)
class NodeSpec:
    name: str
    run: Node
    writes: tuple[str, ...]
    kind: Literal["deterministic", "model"]
    interruptible: bool = False

PIPELINE: tuple[NodeSpec, ...] = (
    intake, stratify, triage, concerns, goals,
    interventions, reconcile, assemble,
)
```

Adding a node becomes one tuple entry plus its function. Removing one becomes
deleting an entry.

### 4.3 Invalidation replaces the index ladder

`revise.py`'s `start == 0 / start <= 1 / start == 2` is replaced by:
re-running node *N* clears every state key written by nodes after *N*, computed
from `writes`. A new node needs no change to the revise loop at all — which is
the concrete test of whether this refactor achieved anything.

### 4.4 The graph

With `state -> partial` nodes and a declared sequence, the runner is
interchangeable:

- **LangGraph** — `StateGraph`, `add_node`, `add_edge`, `compile(checkpointer=...)`.
  Roughly 50 lines, and it brings threads, resume, `interrupt()`, time-travel
  and streaming.
- **Home-grown** — a loop over `PIPELINE` with a state dict. Roughly 80 lines,
  no new dependency, and every one of the above still to write.

**Recommendation: adopt LangGraph, and at stage 2 rather than "later".** The
reasoning is not that frameworks are good; it is that the list of things we
would otherwise hand-write — thread identity, checkpoint storage, interrupt and
resume, replay from a prior state — *is* the framework, and writing a second
one badly is the worse outcome. It was the design's choice from the start, the
dependency is already installed, and the schema already carries the column.

The risk worth naming: LangGraph's state model then constrains ours, and its
checkpointer imposes the serialisation format. That is a real cost and the
reason stage 1 is separate — after stage 1 we can build stage 2 both ways and
compare, having already paid the only cost common to both.

## 5. The staged plan

Each stage ships on its own and leaves the module working.

| Stage | Delivers | How we know it worked |
|---|---|---|
| **0** | Agree the state shape. This document. | Nobody is surprised at stage 1 |
| **1** | Extract `CarePlanState`; nodes become `state -> partial`; `PIPELINE` as data; invalidation by `writes`. **Current runner retained.** | **Every existing test passes unchanged** — that is what makes it a refactor rather than a rewrite |
| **2** | Swap the runner for a compiled `StateGraph`. Same nodes, same state. | Same tests, same eval-harness numbers within the noise floor |
| **3** | Postgres checkpointer; a thread per plan; `checkpoint_thread_id` finally written. | A run can be killed mid-pipeline and resumed |
| **4** | `interrupt()` before assemble; review / approve / edit / reject as graph resumptions; `edits` carry provenance. | Design Phase 4, and the `source` column stops being decorative |
| **5** | The interactive surface: clinician dialogue on `messages`, turns driving graph invocations. | *"Make the second goal measurable"* works |

Stage 1 is the one that must be boring. If existing tests need changing, the
state shape is wrong and stage 0 was not finished.

Stages 4 and 5 are where #88 (agentic UI) and #90 (agent as an MCP tool)
connect — both need a thread and a resumable graph, and neither is buildable
before stage 3.

## 6. What this costs

Stage 1 is roughly 500 lines changed across `plan.py`, `revise.py`, node
signatures and the tests that call nodes directly — mostly mechanical, with the
serialisation of the frozen dataclasses being the only genuinely fiddly part.
Stages 2–3 are small. Stages 4–5 are new capability rather than refactor.

Compared against: hand-writing checkpoint storage, thread identity, interrupt
and resume on the current shape, which is more code, less tested, and ours to
maintain.

## 7. Open questions

1. **Does anything need cross-node LLM context (memory type 3)?** No evidence
   either way. Proposal: make it per-node and off by default, and measure with
   the eval harness if a case appears.
2. **What is the unit of a human edit?** A whole node re-run, or a single
   element mutated in place? The second is what a clinician expects and the
   harder thing to build. Suggest starting with re-run and adding element edits
   at stage 5.
3. **How much history does the grader see?** Grading a plan revised three times
   might legitimately want the objections it already raised, or might be
   corrupted by them. Answerable by the harness once stage 3 lands.
4. **Does the eval harness measure the same thing after stage 2?** It must, and
   if the numbers move outside the noise floor, the refactor changed behaviour
   and is not done.
5. **Do we keep `PlanDraft`?** It overlaps with the state's `concerns`/`goals`/
   `interventions` keys. Probably it becomes a view over state rather than a
   separate accumulator.

## 8. Evidence this design does *not* have

Stated plainly, because the module's history is of plausible designs corrected
by measurement.

- No measurement that message history improves any node's output.
- No measurement that LangGraph's overhead is acceptable at our call volume.
- No prototype of the interactive surface; stage 5 is the least specified.
- The 500-line estimate for stage 1 is a guess from reading, not from doing.

The one thing this design *is* confident about is the diagnosis in §2, which is
read off the code rather than inferred.
