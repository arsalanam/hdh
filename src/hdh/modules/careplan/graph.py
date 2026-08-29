"""Stage 1 of `careplan-state-and-graph.md`: state, declared nodes, a runner.

The pipeline used to live as local variables in ``generate_plan`` and as
index arithmetic in ``revise._build`` — ``start == 0``, ``start <= 1``,
``start == 2``, over exactly three nodes. That shape made three things
expensive at once: checkpointing (nothing to serialise), adding a node
(eight files), and re-entering the pipeline anywhere (no way to express it).

This module changes the shape and nothing else. Same nodes, same order, same
output. The test of whether it worked is boring on purpose: **every existing
test passes unchanged**, and adding a node requires no edit to the revise
loop.

Two decisions are worth finding here.

**State holds data; services hold collaborators.** ``store``, ``selector``
and ``grader`` are not in the state, because state has to survive being
written to a database at stage 3 and a live PostgreSQL session cannot. So a
node takes ``(state, services)`` and returns a *partial* state. That split is
what makes a checkpointer possible later without another refactor.

**Nodes declare what they write, and re-entry invalidates the rest.** Running
from node *N* clears every key written by nodes at or after *N*, computed
from the declarations rather than from an index ladder. A goal written for a
concern that no longer exists is an orphan, and §8's graph invariant does not
permit one — so the invalidation is not tidiness, it is the invariant.

Node 1 (intake) is not in the pipeline either: it needs the ``Patient`` row,
which is not state and should not become state. The caller builds the context
and seeds it, which is what ``generate_plan`` already did.

Node 7 (assemble) is deliberately **not** in the pipeline. The revise loop
grades drafts before writing them, so a pipeline that assembled would write
and delete a plan per round. It becomes a node at stage 4, when the design
needs to interrupt immediately before it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.evaluate import Grader
from hdh.modules.careplan.generate import (
    ConcernDraft,
    GoalDraft,
    InterventionDraft,
    Selector,
    propose_concerns,
    propose_goals,
    propose_interventions,
)
from hdh.modules.careplan.reconcile import ReconcileReport, reconcile
from hdh.modules.careplan.stratify import RiskFlag, stratify
from hdh.modules.careplan.triage import Topic, deferral_lines, triage


class CarePlanState(TypedDict, total=False):
    """Everything a plan run knows.

    Every value here must eventually round-trip through JSON — stage 3 writes
    this to a checkpoint. It does not yet: ``context``, ``flags``, ``topics``
    and the drafts are frozen dataclasses with no encoder. That is stage 3's
    work, and :func:`unserialisable_keys` names the debt so it arrives as a
    checklist rather than a surprise.
    """

    patient_mrn: str

    context: CarePlanContext
    flags: list[RiskFlag]
    topics: list[Topic]
    deferred: list[str]

    concerns: list[ConcernDraft]
    goals: list[GoalDraft]
    raw_interventions: list[InterventionDraft]
    interventions: list[InterventionDraft]
    reconciliation: ReconcileReport | None

    # One key per node rather than one shared list. Re-running a node has to
    # discard that node's old complaints and keep the others', and a single
    # accumulated list cannot express which entry came from where.
    dropped_concerns: list[str]
    dropped_goals: list[str]
    dropped_interventions: list[str]

    #: A grader's objection, for the node about to answer it. Set by the
    #: runner for the starting node only and cleared afterwards, so a later
    #: node never inherits a critique aimed at an earlier one.
    feedback: str


@dataclass(frozen=True)
class PlanServices:
    """The collaborators a run depends on. Not part of state, not persisted."""

    store: object | None = None
    selector: Selector | None = None
    grader: Grader | None = None
    #: A LangGraph checkpointer, or None for an ephemeral run.
    #:
    #: Not built by :meth:`resolved`, deliberately: the durable saver opens
    #: its own database connection, and a factory that did that as a side
    #: effect of a default would open one per plan. The caller builds it
    #: once per session and passes it down.
    checkpointer: object | None = None

    @property
    def selecting(self) -> Selector:
        """The selector, asserted present.

        A node cannot run without one, and the alternative to failing here is
        every adapter re-checking the same thing.
        """
        if self.selector is None:
            raise RuntimeError("no selector — call resolved(session) or inject one")
        return self.selector

    def resolved(self, session) -> PlanServices:
        """The same services with live defaults filled in."""
        store, selector = self.store, self.selector
        if store is None:
            from hdh.modules.careplan.retriever import build_store

            store = build_store(session)
        if selector is None:
            from hdh.modules.careplan.generate import llm_selector

            selector = llm_selector()
        return PlanServices(
            store=store,
            selector=selector,
            grader=self.grader,
            checkpointer=self.checkpointer,
        )


Node = Callable[[CarePlanState, PlanServices], Mapping[str, Any]]


@dataclass(frozen=True)
class NodeSpec:
    """One step, and what it is allowed to change."""

    name: str
    run: Node
    writes: tuple[str, ...]
    kind: Literal["deterministic", "model"]

    @property
    def calls_a_model(self) -> bool:
        return self.kind == "model"


# ── the nodes ────────────────────────────────────────────────────────────
#
# Each is a thin adapter over the function that already existed. The
# functions keep their explicit signatures so they stay directly testable;
# the adapters do the state plumbing. Keeping those separate is why the
# existing tests did not have to change.


def _stratify(state: CarePlanState, _services: PlanServices) -> Mapping[str, Any]:
    return {"flags": stratify(state["context"])}


def _triage(state: CarePlanState, _services: PlanServices) -> Mapping[str, Any]:
    selected, deferred = triage(state["context"], state.get("flags", []))
    return {"topics": selected, "deferred": deferral_lines(deferred)}


def _concerns(state: CarePlanState, services: PlanServices) -> Mapping[str, Any]:
    concerns, dropped = propose_concerns(
        services.store,
        state["context"],
        state.get("flags", []),
        services.selecting,
        state.get("topics", []),
        state.get("feedback", ""),
    )
    return {"concerns": concerns, "dropped_concerns": dropped}


def _goals(state: CarePlanState, services: PlanServices) -> Mapping[str, Any]:
    goals, dropped = propose_goals(
        services.store,
        state["context"],
        state.get("concerns", []),
        services.selecting,
        state.get("feedback", ""),
    )
    return {"goals": goals, "dropped_goals": dropped}


def _interventions(state: CarePlanState, services: PlanServices) -> Mapping[str, Any]:
    drafts, dropped = propose_interventions(
        services.store,
        state["context"],
        state.get("goals", []),
        services.selecting,
        state.get("feedback", ""),
    )
    return {"raw_interventions": drafts, "dropped_interventions": dropped}


def _reconcile(state: CarePlanState, _services: PlanServices) -> Mapping[str, Any]:
    kept, report = reconcile(
        state.get("raw_interventions", []),
        state.get("flags", []),
        goal_count=len(state.get("goals", [])),
    )
    return {"interventions": kept, "reconciliation": report}


#: The generating pipeline, in order. Adding a step is one entry here plus
#: its adapter — and, if the revise loop should be able to route to it, a
#: `revises` value on a rubric dimension. Nothing else.
PIPELINE: tuple[NodeSpec, ...] = (
    NodeSpec("stratify", _stratify, ("flags",), "deterministic"),
    NodeSpec("triage", _triage, ("topics", "deferred"), "deterministic"),
    NodeSpec("concerns", _concerns, ("concerns", "dropped_concerns"), "model"),
    NodeSpec("goals", _goals, ("goals", "dropped_goals"), "model"),
    NodeSpec("interventions", _interventions, ("raw_interventions", "dropped_interventions"), "model"),
    NodeSpec("reconcile", _reconcile, ("interventions", "reconciliation"), "deterministic"),
)


def node_index(name: str) -> int:
    """Where a node sits in the pipeline. Raises on an unknown name."""
    for index, spec in enumerate(PIPELINE):
        if spec.name == name:
            return index
    raise KeyError(f"no pipeline node named {name!r} — have {', '.join(s.name for s in PIPELINE)}")


def written_from(index: int) -> set[str]:
    """Every state key produced by the nodes at or after ``index``."""
    return {key for spec in PIPELINE[index:] for key in spec.writes}


def invalidate_from(state: CarePlanState, index: int) -> CarePlanState:
    """State with everything the nodes from ``index`` produce removed.

    This is the graph invariant, not housekeeping: keeping goals that were
    written for concerns which no longer exist leaves edges pointing at
    nothing.
    """
    doomed = written_from(index)
    return {key: value for key, value in state.items() if key not in doomed}  # type: ignore[return-value]


class MissingUpstream(RuntimeError):
    """Asked to start mid-pipeline without what the earlier nodes produce."""


def require_upstream(state: CarePlanState, start: int) -> None:
    """Refuse to start at ``start`` if an earlier node's output is absent.

    The generic form of a guard the old index ladder had as an assertion:
    rebuilding from ``goals`` with no concerns in hand used to raise. Without
    this the pipeline runs happily and returns an empty plan, which is the
    worse failure — a plan that says nothing was found, when what happened is
    that nobody asked.
    """
    missing = sorted(key for key in written_from(0) - written_from(start) if key not in state)
    if missing:
        raise MissingUpstream(
            f"cannot start at {PIPELINE[start].name!r} — state is missing "
            f"{', '.join(missing)}, produced by the nodes before it"
        )


def run_from(
    state: CarePlanState,
    services: PlanServices,
    start: int = 0,
    *,
    feedback: str = "",
    stop: int | None = None,
) -> CarePlanState:
    """Run the pipeline from ``start`` and return the resulting state.

    An ephemeral compiled graph with no checkpointer — for a caller that
    wants one pass and has nowhere to resume from: tests, and the
    non-durable path. Durable runs go through :func:`compile_pipeline` with
    a real checkpointer and address a thread instead.

    Kept as one runner rather than two implementations of the same walk.
    ``stop`` compiles a truncated pipeline, which is why it stays cheap.
    """
    require_upstream(state, start)
    seed: CarePlanState = dict(invalidate_from(state, start))  # type: ignore[assignment]
    if feedback:
        seed["feedback"] = feedback
    graph = compile_pipeline(pipeline=PIPELINE[start:stop])
    return graph.invoke(seed, context=services)


def dropped(state: CarePlanState) -> list[str]:
    """Every element dropped for lack of evidence, in pipeline order."""
    out: list[str] = []
    for spec in PIPELINE:
        for key in spec.writes:
            if key.startswith("dropped_"):
                out.extend(state.get(key, []))  # type: ignore[arg-type]
    return out


def to_draft(state: CarePlanState):
    """The state's plan elements as the ``PlanDraft`` the writer expects.

    A view rather than a second source of truth — §7 question 5 of the
    design asks whether ``PlanDraft`` survives at all, and this is the answer
    for stage 1: it stays, as a projection, so nothing downstream changes.
    """
    from hdh.modules.careplan.generate import PlanDraft

    return PlanDraft(
        concerns=list(state.get("concerns", [])),
        goals=list(state.get("goals", [])),
        interventions=list(state.get("interventions", [])),
        dropped=dropped(state),
        deferred=list(state.get("deferred", [])),
    )


#: State keys whose values do not yet survive a JSON round trip. Stage 3
#: needs every one of these encodable; naming them here turns that into a
#: checklist rather than a discovery.
UNSERIALISABLE = (
    "context",
    "flags",
    "topics",
    "concerns",
    "goals",
    "raw_interventions",
    "interventions",
    "reconciliation",
)


def unserialisable_keys(state: CarePlanState | Iterable[str]) -> list[str]:
    """Which keys in ``state`` still need an encoder before stage 3."""
    present: Sequence[str] = list(state)
    return [key for key in UNSERIALISABLE if key in present]


# ── the graph ────────────────────────────────────────────────────────────


def _as_graph_node(spec: NodeSpec):
    """Wrap a ``(state, services)`` node as LangGraph's ``(state, runtime)``.

    The node functions never learn they are in a graph. Services arrive as
    the runtime *context* rather than as state, because state is checkpointed
    and a live database session cannot be.

    Every node also clears ``feedback``. A critique is addressed to one node,
    and LangGraph merges rather than replaces — so without this the objection
    would travel down the rest of the pipeline and be answered by the wrong
    step. Whichever node runs first consumes it.
    """

    def node(state: CarePlanState, runtime) -> Mapping[str, Any]:
        produced = dict(spec.run(state, runtime.context))
        produced.setdefault("feedback", "")
        return produced

    node.__name__ = spec.name
    return node


class UndeclaredChannel(RuntimeError):
    """A node writes a state key the schema does not declare."""


def require_declared(specs: Sequence[NodeSpec]) -> None:
    """Every declared write must exist on :class:`CarePlanState`.

    LangGraph **silently discards** keys that are not channels on the state
    schema — no error, no warning, the value simply does not appear. A node
    added without its keys would run, look fine, and produce nothing, which
    is the failure mode this module spends most of its effort refusing.

    So adding a node is two edits, not one: the ``PIPELINE`` entry and its
    keys on ``CarePlanState``. This makes forgetting the second loud.
    """
    declared = set(CarePlanState.__annotations__)
    undeclared = sorted({key for spec in specs for key in spec.writes if key not in declared})
    if undeclared:
        raise UndeclaredChannel(
            f"{', '.join(undeclared)} not declared on CarePlanState — LangGraph would "
            "drop these silently. Add them to the TypedDict alongside the PIPELINE entry."
        )


def review_points(specs: Sequence[NodeSpec]) -> list[str]:
    """The nodes a reviewed run pauses after — the ones that judged.

    Derived from ``kind``, never listed. A node added to ``PIPELINE`` gets
    its review point for free, and cannot be given one it does not deserve.

    Deterministic nodes are deliberately absent. ``stratify``, ``triage`` and
    ``reconcile`` make no judgement to review, and pausing where there is
    nothing to decide teaches a reviewer to press enter without reading —
    which costs more than the pauses are worth.
    """
    return [spec.name for spec in specs if spec.calls_a_model]


def compile_pipeline(checkpointer=None, pipeline: Sequence[NodeSpec] | None = None, *, review: bool = False):
    """A compiled StateGraph over ``PIPELINE``, in declared order.

    Built *from* the declarations rather than replacing them: adding a node
    is still one tuple entry, and the graph is a runner rather than a second
    definition of the pipeline.

    ``review=True`` pauses after every model node (:func:`review_points`).
    It is a property of *how the graph was compiled*, not of what the nodes
    do — deliberately, so the eval harness and a clinician run the same node
    code. A pause implemented inside a node body would mean the thing
    measured is not the thing shipped.
    """
    from langgraph.graph import END, START, StateGraph

    specs = tuple(pipeline if pipeline is not None else PIPELINE)
    if not specs:
        raise ValueError("cannot compile an empty pipeline")
    require_declared(specs)

    if review and checkpointer is None:
        # Not a framework quirk worth passing through raw: pausing means
        # keeping the half-finished run somewhere, and without a saver the
        # graph would stop and lose it.
        raise ValueError(
            "a reviewed run needs a checkpointer — there is nowhere to keep a "
            "paused plan otherwise. Pass one from checkpoints.build(), or "
            "compile with review=False for an unattended run."
        )

    builder = StateGraph(CarePlanState, context_schema=PlanServices)
    for spec in specs:
        builder.add_node(spec.name, _as_graph_node(spec))
    builder.add_edge(START, specs[0].name)
    for earlier, later in zip(specs, specs[1:], strict=False):
        builder.add_edge(earlier.name, later.name)
    builder.add_edge(specs[-1].name, END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=review_points(specs) if review else None,
    )


def thread_config(thread_id: str) -> dict:
    """The config a checkpointed run is addressed by."""
    return {"configurable": {"thread_id": thread_id}}


def resume_at(graph, config: dict, node: str, services: PlanServices, *, feedback: str = ""):
    """Re-run ``node`` and everything after it, keeping what came before.

    LangGraph resumes at the *successors* of ``as_node``, so re-running a
    node means writing state as though its predecessor had just finished.
    Starting at the first node has no predecessor, so it is a fresh run of
    the whole pipeline against the state already in the thread.
    """
    index = node_index(node)
    snapshot = graph.get_state(config)
    require_upstream(snapshot.values, index)

    update = {"feedback": feedback} if feedback else {}
    if index == 0:
        graph.update_state(config, update)
    else:
        graph.update_state(config, update, as_node=PIPELINE[index - 1].name)
    return graph.invoke(None, config, context=services)
