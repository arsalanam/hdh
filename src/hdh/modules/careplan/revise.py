"""M3c: the bounded revise loop.

Design §9's last clause — *"scores below threshold route back to the
relevant section node with the grader's reasons as feedback ... max 2
revision rounds, then proceed to human review regardless"*.

Four decisions are worth finding here rather than inferring.

**The rubric decides where feedback goes.** Each dimension declares the
node it revises (`revises`), so routing is data rather than a table in
code: vague goals are node 4's problem, an unanswered flag is node 5's. The
*earliest* failing node governs, because regenerating concerns while
keeping the goals written for the old ones would leave a graph whose edges
no longer mean anything.

**Rounds are graded before they are written.** A round that scores worse is
discarded, and writing rows only to delete them would leave an audit trail
of plans that never existed. That is what
:func:`~hdh.modules.careplan.facts.evidence_from_draft` is for.

**The best round is kept, not the last.** A revision can make a plan worse
— the model is being told to change something, and change is not
improvement. Best is decided the same way the verdict is: highest minimum
score, then highest mean, then the earliest round, so a revision has to
actually beat what it replaced rather than merely tie it.

**Nothing here approves anything.** The loop ends and a human reviews,
whether it converged or not. Both the rounds that were rejected and the
reason each one ran are kept, because a reviewer looking at a plan that
took three attempts should be able to see the two it beat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.evaluate import Evaluation, Grader, evaluate
from hdh.modules.careplan.facts import evidence_from_draft
from hdh.modules.careplan.generate import PlanDraft, Selector
from hdh.modules.careplan.graph import (
    CarePlanState,
    compile_pipeline,
    node_index,
    resume_at,
    run_from,
    thread_config,
    to_draft,
)
from hdh.modules.careplan.graph import (
    PlanServices as GraphServices,
)
from hdh.modules.careplan.reconcile import ReconcileReport
from hdh.modules.careplan.rubric import REVISABLE_NODES, Rubric, select_rubric
from hdh.modules.careplan.stratify import RiskFlag
from hdh.modules.careplan.triage import Topic

#: How many times a plan may be sent back. §9 says two, and the number
#: matters more than it looks: each round is a full regeneration plus a
#: full re-grade, so an unbounded loop is an unbounded bill, and a model
#: told repeatedly to "do better" starts changing things that were right.
MAX_REVISION_ROUNDS = 2

#: Separator between the objections handed to one node.
JOIN = "\n\n"


@dataclass(frozen=True)
class PlanInputs:
    """The chart a run starts from, and the collaborators it uses.

    Retained as the caller-facing shape, but it is now a *seed* rather than
    an argument bundle: :meth:`seed` turns it into the graph state, and the
    nodes read that instead of taking six threaded parameters.
    """

    store: object
    context: CarePlanContext
    flags: tuple[RiskFlag, ...]
    topics: tuple[Topic, ...]
    selector: Selector
    deferred: tuple[str, ...] = ()

    #: The thread this run's checkpoints belong to. Empty means "no thread":
    #: an ephemeral in-memory graph, which is what tests and one-shot runs
    #: want. A durable run supplies one and can be resumed against it.
    thread_id: str = ""
    #: A LangGraph checkpointer. ``None`` pairs with an empty thread_id.
    checkpointer: object | None = None

    def seed(self) -> CarePlanState:
        """The state a run begins with, before any node has run."""
        return {
            "context": self.context,
            "flags": list(self.flags),
            "topics": list(self.topics),
            "deferred": list(self.deferred),
        }

    def services(self) -> GraphServices:
        return GraphServices(store=self.store, selector=self.selector)


@dataclass(frozen=True)
class Round:
    """One attempt, what prompted it, and how it scored."""

    number: int
    draft: PlanDraft
    reconciliation: ReconcileReport | None
    evaluation: Evaluation
    node: str = ""
    feedback: tuple[str, ...] = ()

    def rank(self) -> tuple[int, float]:
        """Higher is better, by the rule the verdict already uses."""
        scored = [score.score for score in self.evaluation.graded if score.score is not None]
        if not scored:
            return (-1, -1.0)
        return (min(scored), self.evaluation.overall or 0.0)


@dataclass
class RevisionLog:
    """Every attempt, and which one was kept."""

    rounds: list[Round] = field(default_factory=list)
    kept: int = 0

    @property
    def best(self) -> Round:
        return self.rounds[self.kept]

    @property
    def improved(self) -> bool:
        return self.kept > 0

    def as_lines(self, rubric: Rubric) -> list[str]:
        """One line per attempt, marking the one that was kept."""
        lines = []
        for entry in self.rounds:
            mark = "kept" if entry.number == self.kept else "    "
            verdict = entry.evaluation.verdict(rubric)
            mean = entry.evaluation.overall
            worst = entry.evaluation.lowest
            detail = f"lowest {worst.dimension_id} {worst.score}" if worst else "nothing graded"
            reason = f" after revising {entry.node}" if entry.node else ""
            lines.append(
                f"  [{mark}] round {entry.number}{reason}: {verdict}, "
                f"mean {mean if mean is not None else '—'}, {detail}"
            )
        if not self.improved and len(self.rounds) > 1:
            lines.append("  no revision beat the first attempt — the first was kept")
        return lines


def _failing(evaluation: Evaluation, rubric: Rubric) -> list:
    """Graded dimensions scoring below the rubric's revise threshold."""
    by_id = {dimension.id: dimension for dimension in rubric.dimensions}
    failing = []
    for score in evaluation.graded:
        dimension = by_id.get(score.dimension_id)
        if dimension is not None and score.score is not None and score.score < rubric.revise_below:
            failing.append((dimension, score))
    return failing


def route(evaluation: Evaluation, rubric: Rubric) -> tuple[str, list[str]]:
    """Which node to send this back to, and what to tell it.

    The earliest failing node wins. Feedback from *every* dimension routed
    to that node travels with it — a node asked to fix one objection while
    a second goes unmentioned will trade one for the other.
    """
    failing = _failing(evaluation, rubric)
    if not failing:
        return "", []
    node = min(dimension.node_order for dimension, _score in failing)
    name = REVISABLE_NODES[node]
    notes = [
        f"{dimension.title} scored {score.score} of {rubric.scale_max}. {score.justification}".strip()
        for dimension, score in failing
        if dimension.revises == name
    ]
    return name, notes


def revise_plan(
    inputs: PlanInputs,
    grader: Grader,
    *,
    rubric: Rubric | None = None,
    max_rounds: int = MAX_REVISION_ROUNDS,
) -> tuple[Rubric, RevisionLog]:
    """Generate, grade, and send back up to ``max_rounds`` times.

    Returns the rubric used and the full log. The caller writes the kept
    round; nothing here touches the database.
    """
    rubric = rubric or select_rubric(inputs.context)
    log = RevisionLog()

    node: str = "concerns"
    notes: list[str] = []
    services = inputs.services()

    # One compiled graph for the whole loop. With a checkpointer and a
    # thread, each round is a *resume* rather than a re-run: LangGraph keeps
    # the state between invocations, so re-entering at a node replays only
    # that node onward. Without one, every round is an independent pass over
    # a state we carry ourselves — same results, nothing to come back to.
    graph = compile_pipeline(inputs.checkpointer)
    config = thread_config(inputs.thread_id) if inputs.thread_id else None
    state: CarePlanState = inputs.seed()

    for number in range(max_rounds + 1):
        feedback = JOIN.join(notes)
        if config is None:
            state = run_from(state, services, node_index(node), feedback=feedback)
        elif number == 0:
            state = graph.invoke(inputs.seed(), config, context=services)
        else:
            # Re-entry, not reconstruction. Which node comes from the rubric,
            # so a new node needs no change here.
            state = resume_at(graph, config, node, services, feedback=feedback)
        draft = to_draft(state)
        reconciliation = state.get("reconciliation")
        evidence = evidence_from_draft(inputs.context, draft, inputs.flags, reconciliation, inputs.deferred)
        _rubric, evaluation = evaluate(evidence, grader, rubric=rubric)
        log.rounds.append(
            Round(
                number=number,
                draft=draft,
                reconciliation=reconciliation,
                evaluation=evaluation,
                node="" if number == 0 else node,
                feedback=tuple(notes),
            )
        )

        node, notes = route(evaluation, rubric)
        if not node:
            break  # nothing below threshold — there is nothing to send back

    # Best, not last. A revision has to beat what it replaced, and ties go
    # to the earlier round so a change that achieved nothing is not adopted.
    log.kept = max(range(len(log.rounds)), key=lambda index: (log.rounds[index].rank(), -index))
    return rubric, log
