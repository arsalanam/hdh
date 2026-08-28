"""Stage 1: state, declared nodes, and re-entry.

The refactor's whole claim is that three things got cheaper — checkpointing,
adding a node, and re-entering the pipeline. Two of those are testable now,
and the third (checkpointing) has its debt named rather than paid.

The most important test in this file is
`test_adding_a_node_needs_no_change_to_re_entry`. Before this refactor,
`revise.py` walked an index ladder over exactly three nodes — `start == 0`,
`start <= 1`, `start == 2` — so a fourth node meant rewriting it. If that
test can add a node and re-enter correctly without touching anything, the
refactor did what it was for. If it cannot, this was churn.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import graph
from hdh.modules.careplan.context import CarePlanContext, ProblemView
from hdh.modules.careplan.graph import (
    PIPELINE,
    MissingUpstream,
    NodeSpec,
    PlanServices,
    dropped,
    invalidate_from,
    node_index,
    run_from,
    to_draft,
    unserialisable_keys,
    written_from,
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


def _services():
    return PlanServices(store=FakeStore(), selector=_selector)


def _seed():
    return {"context": _context()}


@pytest.fixture()
def restore_pipeline():
    """Tests that add a node must not leak it into the others."""
    saved = graph.PIPELINE
    yield
    graph.PIPELINE = saved


# ── the pipeline runs, and produces what it used to ──────────────────────


def test_a_full_run_produces_every_stage():
    state = run_from(_seed(), _services())
    assert {"flags", "topics", "concerns", "goals", "interventions", "reconciliation"} <= set(state)


def test_the_draft_is_a_view_over_state_not_a_second_source():
    state = run_from(_seed(), _services())
    draft = to_draft(state)
    assert draft.concerns == state["concerns"]
    assert draft.interventions == state["interventions"]
    assert draft.deferred == state["deferred"]


def test_every_node_declares_what_it_writes():
    """`writes` is what invalidation and the upstream guard are computed
    from. A node that under-declares leaves stale state behind."""
    for spec in PIPELINE:
        state = run_from(_seed(), _services(), stop=node_index(spec.name) + 1)
        produced = set(state) - {"context", "feedback"}
        declared = written_from(0) - written_from(node_index(spec.name) + 1)
        assert produced <= declared, f"{spec.name} wrote something it did not declare"


# ── re-entry ─────────────────────────────────────────────────────────────


def test_re_entry_keeps_what_came_before_and_discards_what_came_after():
    """The graph invariant, not housekeeping. Keeping goals written for
    concerns that no longer exist leaves edges pointing at nothing."""
    full = run_from(_seed(), _services())
    trimmed = invalidate_from(full, node_index("goals"))
    assert "concerns" in trimmed and "flags" in trimmed
    assert "goals" not in trimmed
    assert "interventions" not in trimmed and "reconciliation" not in trimmed


def test_re_entering_late_does_not_re_run_the_early_nodes():
    """Re-entry is the reason model calls are affordable at all: revising the
    interventions must not pay for the concerns and goals again."""
    calls: list[int] = []

    def counting(task):
        calls.append(1)
        return _selector(task)

    services = PlanServices(store=FakeStore(), selector=counting)
    full = run_from(_seed(), services)
    whole_run = len(calls)

    calls.clear()
    run_from(full, services, node_index("interventions"))
    assert 0 < len(calls) < whole_run, "re-entry cost as much as a full run"


def test_starting_mid_pipeline_without_upstream_output_refuses():
    with pytest.raises(MissingUpstream, match="concerns"):
        run_from(
            {"context": _context(), "flags": [], "topics": [], "deferred": []},
            _services(),
            node_index("goals"),
        )


def test_feedback_reaches_only_the_node_it_starts_at():
    """A critique of the goals handed on to the interventions node would be
    answered by the wrong step."""
    seen: list[str] = []

    def watching(task):
        seen.append(task.instruction)
        return _selector(task)

    services = PlanServices(store=FakeStore(), selector=watching)
    full = run_from(_seed(), services)
    seen.clear()
    run_from(full, services, node_index("goals"), feedback="Goals were vague.")
    assert any("Goals were vague." in instruction for instruction in seen)
    assert sum("Goals were vague." in instruction for instruction in seen) == 1


# ── the point of the whole exercise ──────────────────────────────────────


def test_adding_a_node_needs_no_change_to_re_entry(restore_pipeline):
    """The test that says whether this refactor was worth doing.

    Before it, `revise.py` walked `start == 0` / `start <= 1` / `start == 2`
    over exactly three nodes, so a fourth meant rewriting the loop. Here a
    node is added at runtime and re-entry, invalidation and the upstream
    guard all keep working, with nothing edited.
    """

    def _summarise(state, _services):
        return {"summary": f"{len(state.get('interventions', []))} interventions"}

    graph.PIPELINE = (*PIPELINE, NodeSpec("summarise", _summarise, ("summary",), "deterministic"))

    state = run_from(_seed(), _services())
    assert state["summary"].endswith("interventions")

    # Invalidation understands the new node without being told.
    assert "summary" in written_from(graph.node_index("summarise"))
    trimmed = invalidate_from(state, graph.node_index("goals"))
    assert "summary" not in trimmed

    # And re-entering an earlier node regenerates it.
    again = run_from(state, _services(), graph.node_index("goals"))
    assert "summary" in again


def test_removing_a_node_is_also_just_the_tuple(restore_pipeline):
    graph.PIPELINE = tuple(spec for spec in PIPELINE if spec.name != "reconcile")
    state = run_from(_seed(), _services())
    assert "raw_interventions" in state
    assert "reconciliation" not in state


def test_an_unknown_node_name_says_what_exists():
    with pytest.raises(KeyError, match="concerns"):
        node_index("nonesuch")


# ── bookkeeping ──────────────────────────────────────────────────────────


def test_dropped_is_collected_per_node_not_accumulated():
    """One shared list could not express which entry came from where, so
    re-running a node would either lose other nodes' complaints or keep its
    own stale ones."""
    state = run_from(_seed(), _services())
    assert isinstance(dropped(state), list)
    assert {"dropped_concerns", "dropped_goals", "dropped_interventions"} <= set(state)


def test_services_are_not_part_of_state():
    """State has to survive being written to a database at stage 3, and a
    live PostgreSQL session cannot. Keeping collaborators out is what makes
    a checkpointer possible without another refactor."""
    state = run_from(_seed(), _services())
    assert "store" not in state and "selector" not in state and "grader" not in state


def test_the_serialisation_debt_is_named():
    """Stage 3 needs every state value JSON-encodable. None of these are yet,
    and listing them turns that into a checklist rather than a discovery."""
    state = run_from(_seed(), _services())
    outstanding = unserialisable_keys(state)
    assert "context" in outstanding and "concerns" in outstanding
    assert "deferred" not in outstanding, "plain strings already round-trip"


def test_model_nodes_are_marked_as_such():
    kinds = {spec.name: spec.calls_a_model for spec in PIPELINE}
    assert kinds == {
        "stratify": False,
        "triage": False,
        "concerns": True,
        "goals": True,
        "interventions": True,
        "reconcile": False,
    }
