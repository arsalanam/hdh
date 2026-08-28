"""Care plan, milestone 3c: the bounded revise loop.

Design §9's last clause. Everything here runs offline — the selector and
the grader are both injected, so a loop that would cost a full
regeneration per round costs nothing in CI.

The properties worth pinning are not "does it revise" but the four things
that make revising safe: it terminates, it keeps the best rather than the
last, it sends feedback to the node that caused the problem, and it never
decides anything.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan.context import CarePlanContext, ProblemView
from hdh.modules.careplan.evaluate import PASS, stub_grader
from hdh.modules.careplan.generate import Selector
from hdh.modules.careplan.knowledge import KnowledgeHit
from hdh.modules.careplan.revise import (
    MAX_REVISION_ROUNDS,
    PlanInputs,
    revise_plan,
    route,
)
from hdh.modules.careplan.rubric import load_rubrics
from hdh.modules.careplan.triage import triage

DIMENSIONS = (
    "completeness",
    "traceability",
    "guideline_concordance",
    "safety",
    "goal_quality",
    "feasibility_burden",
)


def _rubric(name: str = "default"):
    return next(r for r in load_rubrics() if r.rubric_id == name)


def _context():
    return CarePlanContext(
        mrn="TEST01",
        age=52,
        sex="FEMALE",
        problems=(ProblemView("E11.9", "Type 2 diabetes mellitus", False, None),),
    )


class FakeStore:
    """Always returns one citable chunk."""

    def search(self, query, corpus, k=5, filters=None):
        return [
            KnowledgeHit(
                corpus="med_safety",
                doc_id="doc",
                chunk="Text of doc.",
                score=0.5,
                source="notes",
                license="MIT",
                metadata={},
            )
        ][:k]


def _selector() -> Selector:
    """Answers every node, forever — rounds re-ask the same questions."""
    cite = ["med_safety/doc"]

    def select(task):
        properties = task.schema["properties"]["selections"]["items"]["properties"]
        item = {"statement": "A statement", "cites": cite}
        if "concern_type" in properties:
            item["concern_type"] = "condition"
        if "concern_index" in properties:
            item["concern_index"] = 0
            item["target_value"] = ""
        if "goal_index" in properties:
            item["goal_index"] = 0
            item["intervention_type"] = "monitoring"
            item["owner_role"] = "GP"
        return {"selections": [item]}

    return select


def _run(scores_by_round, rubric=None, max_rounds=MAX_REVISION_ROUNDS):
    """Run the loop with a grader that answers differently each round."""
    rounds = iter(scores_by_round)
    current = {"scores": next(rounds)}

    def grader(task):
        answer = stub_grader(current["scores"])(task)
        if task.dimension.id == DIMENSIONS[-1]:
            current["scores"] = next(rounds, current["scores"])
        return answer

    context = _context()
    topics, _deferred = triage(context, [])
    return revise_plan(
        PlanInputs(
            store=FakeStore(),
            context=context,
            flags=(),
            topics=tuple(topics),
            selector=_selector(),
        ),
        grader,
        rubric=rubric or _rubric(),
        max_rounds=max_rounds,
    )


# ── termination ──────────────────────────────────────────────────────────


def test_a_passing_first_attempt_is_not_revised():
    """Nothing below threshold means nothing to send back. A loop that
    revised anyway would spend a full regeneration to change a plan that
    was already good enough."""
    rubric, log = _run([dict.fromkeys(DIMENSIONS, 5)])
    assert len(log.rounds) == 1
    assert log.best.evaluation.verdict(rubric) == PASS


def test_a_plan_that_never_improves_stops_at_the_bound():
    """§9 says max two revision rounds. A model told repeatedly to do
    better starts changing things that were right, and an unbounded loop is
    an unbounded bill."""
    _rub, log = _run([dict.fromkeys(DIMENSIONS, 2)] * 6)
    assert len(log.rounds) == MAX_REVISION_ROUNDS + 1


def test_the_bound_is_configurable_and_respected():
    _rub, log = _run([dict.fromkeys(DIMENSIONS, 2)] * 6, max_rounds=1)
    assert len(log.rounds) == 2


def test_the_loop_stops_early_once_it_clears_the_threshold():
    poor = dict.fromkeys(DIMENSIONS, 2)
    good = dict.fromkeys(DIMENSIONS, 5)
    _rub, log = _run([poor, good, good])
    assert len(log.rounds) == 2, "it kept revising after clearing the bar"


# ── best, not last ───────────────────────────────────────────────────────


def test_the_best_round_is_kept_not_the_last():
    """The most important test in this file.

    A revision can make a plan worse — the model is being told to change
    something, and change is not improvement. Taking the last round on
    faith would let a worse plan replace a better one for no reason beyond
    recency.
    """
    good = dict.fromkeys(DIMENSIONS, 3)
    worse = dict.fromkeys(DIMENSIONS, 2)
    _rub, log = _run([good, worse, worse])
    assert log.kept == 0
    assert log.best.evaluation.overall == 3.0
    assert not log.improved


def test_an_improvement_is_adopted():
    poor = dict.fromkeys(DIMENSIONS, 2)
    better = dict.fromkeys(DIMENSIONS, 3)
    _rub, log = _run([poor, better, better])
    assert log.kept == 1
    assert log.improved


def test_a_tie_goes_to_the_earlier_round():
    """A revision has to beat what it replaced, not merely match it —
    otherwise a change that achieved nothing gets adopted anyway."""
    same = dict.fromkeys(DIMENSIONS, 2)
    _rub, log = _run([same, same, same])
    assert log.kept == 0


def test_ranking_uses_the_minimum_before_the_mean():
    """The same rule the verdict uses. A round that lifted five dimensions
    while dropping safety is not an improvement."""
    balanced = dict.fromkeys(DIMENSIONS, 3)
    lopsided = dict.fromkeys(DIMENSIONS, 5)
    lopsided["safety"] = 1
    _rub, log = _run([balanced, lopsided, lopsided])
    assert log.rounds[1].evaluation.overall > log.rounds[0].evaluation.overall
    assert log.kept == 0, "a higher mean with a worse minimum was adopted"


# ── routing ──────────────────────────────────────────────────────────────


def test_feedback_goes_to_the_node_the_rubric_names():
    rubric = _rubric()
    _rub, log = _run([{**dict.fromkeys(DIMENSIONS, 5), "goal_quality": 2}] * 3, rubric=rubric)
    assert rubric.dimension("goal_quality").revises == "goals"
    assert log.rounds[1].node == "goals"


def test_the_earliest_failing_node_governs():
    """Regenerating concerns while keeping goals written for the old ones
    would leave a graph whose edges no longer mean anything."""
    rubric = _rubric()
    scores = dict.fromkeys(DIMENSIONS, 5)
    scores["goal_quality"] = 2  # -> goals
    scores["completeness"] = 2  # -> concerns, which is earlier
    _rub, log = _run([scores] * 3, rubric=rubric)
    assert log.rounds[1].node == "concerns"


def test_every_objection_for_that_node_travels_with_it():
    """A node asked to fix one objection while a second goes unmentioned
    will trade one for the other."""
    rubric = _rubric()
    evaluation = _run([{**dict.fromkeys(DIMENSIONS, 5), "safety": 2, "feasibility_burden": 2}])[1]
    node, notes = route(evaluation.rounds[-1].evaluation, rubric)
    assert node == "interventions"
    assert len(notes) == 2


def test_a_passing_evaluation_routes_nowhere():
    rubric = _rubric()
    _rub, log = _run([dict.fromkeys(DIMENSIONS, 5)], rubric=rubric)
    node, notes = route(log.best.evaluation, rubric)
    assert node == "" and notes == []


def test_the_feedback_reaches_the_node_that_has_to_answer_it():
    """Threaded into the instruction, not left in the log."""
    seen: list[str] = []

    def watching_selector(task):
        seen.append(task.instruction)
        return _selector()(task)

    rounds = iter([{**dict.fromkeys(DIMENSIONS, 5), "goal_quality": 2}, dict.fromkeys(DIMENSIONS, 5)])
    current = {"scores": next(rounds)}

    def grader(task):
        answer = stub_grader(current["scores"])(task)
        if task.dimension.id == DIMENSIONS[-1]:
            current["scores"] = next(rounds, current["scores"])
        return answer

    context = _context()
    topics, _deferred = triage(context, [])
    revise_plan(
        PlanInputs(
            store=FakeStore(),
            context=context,
            flags=(),
            topics=tuple(topics),
            selector=watching_selector,
        ),
        grader,
        rubric=_rubric(),
    )
    assert any("scored below the required standard" in instruction for instruction in seen)
    assert any("Goal quality scored 2" in instruction for instruction in seen)


# ── what the loop does not do ────────────────────────────────────────────


def test_the_loop_writes_nothing():
    """Rounds are graded as drafts. Writing rows only to delete them would
    leave an audit trail of plans that never existed — and `revise_plan`
    takes no session, so it could not write even by accident."""
    import inspect

    assert "session" not in inspect.signature(revise_plan).parameters


def test_every_round_is_kept_for_the_reviewer():
    """A reviewer looking at a plan that took three attempts should be able
    to see the two it beat, and why each one ran."""
    poor = dict.fromkeys(DIMENSIONS, 2)
    rubric = _rubric()
    _rub, log = _run([poor] * 4, rubric=rubric)
    assert len(log.rounds) == 3
    assert all(entry.evaluation.scores for entry in log.rounds)
    assert log.rounds[1].feedback and log.rounds[2].feedback
    lines = log.as_lines(rubric)
    assert sum(line.startswith("  [kept]") for line in lines) == 1, "exactly one round is the kept one"


def test_the_log_says_when_no_revision_helped():
    poor = dict.fromkeys(DIMENSIONS, 2)
    rubric = _rubric()
    _rub, log = _run([poor] * 4, rubric=rubric)
    assert any("no revision beat the first attempt" in line for line in log.as_lines(rubric))


def test_carrying_forward_a_later_node_needs_the_earlier_output():
    """A guard against a caller asking for a partial rebuild with nothing to
    rebuild from.

    The old index ladder asserted this directly. The graph runner has to
    state it generically — and without the guard it runs happily and returns
    an *empty plan*, which is the worse failure: a plan reporting that nothing
    was found, when what happened is that nobody asked.
    """
    from hdh.modules.careplan.graph import MissingUpstream, node_index, run_from
    from hdh.modules.careplan.graph import PlanServices as GraphServices

    services = GraphServices(store=FakeStore(), selector=_selector())
    seed = {"context": _context(), "flags": [], "topics": [], "deferred": []}
    with pytest.raises(MissingUpstream, match="concerns"):
        run_from(seed, services, node_index("goals"))
