"""S4a of `interactive-care-planning.md`: the graph pauses for a reviewer.

The property that matters is not "it stops". It is that **a rejected stage
has not yet shaped anything** — the stages beneath it were never computed,
because the graph stopped before them. Reviewing a finished plan cannot
offer that, and it is the whole reason review happens between nodes rather
than at the end.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import review
from hdh.modules.careplan.context import CarePlanContext, ProblemView
from hdh.modules.careplan.graph import (
    PIPELINE,
    PlanServices,
    compile_pipeline,
    review_points,
    thread_config,
)
from hdh.modules.careplan.knowledge import KnowledgeHit


class FakeStore:
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


def _selector(task):
    properties = task.schema["properties"]["selections"]["items"]["properties"]
    item = {"statement": "A statement", "cites": ["med_safety/doc"]}
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


def _context():
    return CarePlanContext(
        mrn="TEST01",
        age=70,
        sex="MALE",
        problems=(ProblemView("E11.9", "Type 2 diabetes mellitus", False, None),),
    )


@pytest.fixture()
def reviewed():
    """A reviewed graph on an in-memory checkpointer, and its thread."""
    from langgraph.checkpoint.memory import InMemorySaver

    services = PlanServices(store=FakeStore(), selector=_selector)
    graph = compile_pipeline(checkpointer=InMemorySaver(), review=True)
    return graph, thread_config("review-thread"), services


# ── where it pauses, and where it does not ───────────────────────────────


def test_it_pauses_after_the_nodes_that_judged():
    assert review_points(PIPELINE) == ["concerns", "goals", "interventions"]


def test_it_does_not_pause_where_there_is_nothing_to_decide():
    """Pausing on a deterministic node teaches the reviewer to press enter
    without reading, which costs more than the pause is worth."""
    paused = set(review_points(PIPELINE))
    for spec in PIPELINE:
        if spec.kind == "deterministic":
            assert spec.name not in paused, f"{spec.name} makes no judgement to review"


def test_the_pause_set_is_derived_not_listed():
    """A node added to PIPELINE gets its review point for free."""
    from dataclasses import replace

    invented = replace(PIPELINE[2], name="invented")
    assert "invented" in review_points((*PIPELINE, invented))


def test_a_reviewed_run_needs_somewhere_to_keep_the_paused_plan():
    with pytest.raises(ValueError, match="needs a checkpointer"):
        compile_pipeline(review=True)


def test_an_unattended_run_never_pauses():
    """The harness and the clinician must run the same node code, so review
    is a property of the compile, not of the nodes."""
    from langgraph.checkpoint.memory import InMemorySaver

    services = PlanServices(store=FakeStore(), selector=_selector)
    graph = compile_pipeline(checkpointer=InMemorySaver(), review=False)
    config = thread_config("unattended")
    graph.invoke({"context": _context()}, config, context=services)
    assert review.where(graph, config).finished


# ── the three verbs ──────────────────────────────────────────────────────


def test_the_first_pause_is_after_concerns(reviewed):
    graph, config, services = reviewed
    pause = review.begin(graph, config, {"context": _context()}, services)
    assert pause.node == "concerns"
    assert pause.next_node == "goals"
    assert not pause.finished
    assert pause.proposed["concerns"]


def test_approving_advances_to_the_next_judgement(reviewed):
    graph, config, services = reviewed
    review.begin(graph, config, {"context": _context()}, services)
    assert review.approve(graph, config, services).node == "goals"
    assert review.approve(graph, config, services).node == "interventions"


def test_approving_to_the_end_finishes_the_plan(reviewed):
    graph, config, services = reviewed
    review.begin(graph, config, {"context": _context()}, services)
    pause = review.run_to_end(graph, config, services)
    assert pause.finished
    assert pause.values["interventions"]


def test_nothing_left_to_approve_says_so(reviewed):
    graph, config, services = reviewed
    review.begin(graph, config, {"context": _context()}, services)
    review.run_to_end(graph, config, services)
    with pytest.raises(review.ReviewError, match="nothing left to approve"):
        review.approve(graph, config, services)


def test_an_edit_enters_where_the_model_output_did(reviewed):
    """The clinician's judgement replaces the node's, rather than being
    layered on top of it — so the next node reads the edited list."""
    graph, config, services = reviewed
    pause = review.begin(graph, config, {"context": _context()}, services)
    kept = list(pause.proposed["concerns"])[:0]  # drop them all
    after = review.edit(graph, config, services, concerns=kept)
    assert after.values["concerns"] == []


def test_an_edit_that_would_vanish_is_refused(reviewed):
    """LangGraph drops undeclared state keys in silence. An edit that
    disappears is worse than one that fails: the reviewer believes it took."""
    graph, config, services = reviewed
    review.begin(graph, config, {"context": _context()}, services)
    with pytest.raises(review.ReviewError, match="not part of plan state"):
        review.edit(graph, config, services, concerms=[])


def test_a_rejection_needs_a_reason(reviewed):
    graph, config, services = reviewed
    review.begin(graph, config, {"context": _context()}, services)
    with pytest.raises(review.ReviewError, match="needs a reason"):
        review.reject(graph, config, services, feedback="   ")


def test_a_rejected_stage_runs_again_and_hears_why(reviewed):
    graph, config, services = reviewed
    seen: list[str] = []

    def watching(task):
        # Feedback reaches a node folded into its instruction, via
        # `generate._instruct` — not as a separate field.
        seen.append(task.instruction)
        return _selector(task)

    services = PlanServices(store=FakeStore(), selector=watching)
    review.begin(graph, config, {"context": _context()}, services)
    pause = review.reject(graph, config, services, feedback="too many, and none are urgent")
    assert pause.node == "concerns", "rejection should leave us reviewing concerns again"
    assert any("too many" in entry for entry in seen)


# ── the property this design exists for ──────────────────────────────────


def test_rejecting_a_concern_costs_nothing_downstream(reviewed):
    """The reason review happens between nodes and not at the end.

    At the first pause the goals have never been computed, so a concern sent
    back cannot have shaped one. Reviewing a finished plan cannot offer this
    — by then every goal was written under the concern being rejected.
    """
    graph, config, services = reviewed
    calls: list[str] = []

    def counting(task):
        # Which stage asked is readable from the schema it demands: only
        # goals carry a concern_index, only interventions a goal_index.
        properties = task.schema["properties"]["selections"]["items"]["properties"]
        stage = (
            "interventions"
            if "goal_index" in properties
            else "goals"
            if "concern_index" in properties
            else "concerns"
        )
        calls.append(stage)
        return _selector(task)

    services = PlanServices(store=FakeStore(), selector=counting)
    pause = review.begin(graph, config, {"context": _context()}, services)

    assert not pause.values.get("goals"), "goals must not exist at the concerns pause"
    before = len(calls)
    review.reject(graph, config, services, feedback="start again")
    assert not review.where(graph, config).values.get("goals")
    assert calls[before:] == ["concerns"], "only concerns should have re-run"


# ── what the reviewer is shown ───────────────────────────────────────────


def test_the_reviewer_sees_what_was_withheld_not_just_what_was_kept(reviewed):
    """A reviewer who cannot see what was withheld is reviewing a filtered
    list without knowing it, which produces confidence rather than review."""
    graph, config, services = reviewed
    pause = review.begin(graph, config, {"context": _context()}, services)
    assert "deferred" in pause.withheld
    assert any(key.startswith("dropped_") for key in pause.withheld)


def test_the_summary_names_the_stage_and_its_citations(reviewed):
    graph, config, services = reviewed
    pause = review.begin(graph, config, {"context": _context()}, services)
    text = "\n".join(review.summarise(pause))
    assert "paused after concerns" in text
    assert "next: goals" in text
    assert "cites med_safety/doc" in text
