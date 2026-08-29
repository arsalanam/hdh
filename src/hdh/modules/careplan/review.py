"""Stage 4 of `interactive-care-planning.md`: a plan a clinician can steer.

A reviewed run stops after every node that made a judgement, shows what it
proposed and what it withheld, and waits. Three verbs move it on: approve,
edit, reject.

**Why the pause is before the next node rather than at the end.** Reviewing
a finished plan can only tell you that it is wrong. Reviewing it stage by
stage tells you *where* — and more importantly, a concern rejected here has
not yet shaped anything, because the goals beneath it were never computed.
The graph had not reached them.

**What the reviewer must see.** Not the state dict. At each pause: what was
proposed, what each item cites, what the model dropped, and what triage
deferred before the model saw anything. A reviewer who cannot see what was
*withheld* is reviewing a filtered list without knowing it — which is worse
than not reviewing, because it produces confidence.

Nothing here is a new mechanism. The pause is ``interrupt_after`` from
:func:`~hdh.modules.careplan.graph.compile_pipeline`; approve is a bare
resume; edit is ``update_state`` as the node that just ran; reject is the
revise loop's :func:`~hdh.modules.careplan.graph.resume_at`. This module is
the vocabulary, not the machinery.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hdh.modules.careplan.graph import (
    PIPELINE,
    CarePlanState,
    NodeSpec,
    PlanServices,
    resume_at,
)


class ReviewError(RuntimeError):
    """A review verb was used where it cannot apply."""


def _specs(pipeline: Sequence[NodeSpec] | None) -> tuple[NodeSpec, ...]:
    return tuple(pipeline if pipeline is not None else PIPELINE)


@dataclass(frozen=True)
class Pause:
    """Where a reviewed run stopped, and everything there is to look at.

    ``node`` is what just finished — the thing under review. ``next_node`` is
    what runs on approval, and is ``None`` when the run is complete. Both are
    derived from the graph's own view of what comes next, so they cannot
    drift from where the run actually is.
    """

    node: str | None
    next_node: str | None
    values: Mapping[str, Any]

    @property
    def finished(self) -> bool:
        """No node left to run. There is nothing to approve."""
        return self.next_node is None

    @property
    def started(self) -> bool:
        return self.node is not None

    @property
    def proposed(self) -> Mapping[str, Any]:
        """What the paused node wrote, by channel.

        Read through the node's own ``writes`` declaration rather than a
        hardcoded key per node, so this keeps working for a node nobody has
        written yet.
        """
        if self.node is None:
            return {}
        spec = next((s for s in PIPELINE if s.name == self.node), None)
        if spec is None:
            return {}
        return {key: self.values.get(key) for key in spec.writes}

    @property
    def withheld(self) -> Mapping[str, Any]:
        """What never reached the reviewer's list, and why it did not.

        Two different absences, kept apart because they have different
        causes: ``dropped_*`` is the model declining a candidate it was
        offered, and ``deferred`` is triage removing a problem before the
        model saw anything at all. Presenting them as one list would hide
        that a problem was never considered.
        """
        dropped = {key: value for key, value in self.proposed.items() if key.startswith("dropped_")}
        return {**dropped, "deferred": self.values.get("deferred") or []}


def _pause_from(graph, config: dict, specs: Sequence[NodeSpec]) -> Pause:
    """Read the graph's position and turn it into something reviewable."""
    snapshot = graph.get_state(config)
    names = [spec.name for spec in specs]
    upcoming = [name for name in (snapshot.next or ()) if name in names]
    next_node = upcoming[0] if upcoming else None

    if next_node is None:
        node = names[-1] if snapshot.values else None
    else:
        index = names.index(next_node)
        node = names[index - 1] if index else None
    return Pause(node=node, next_node=next_node, values=dict(snapshot.values or {}))


def begin(graph, config: dict, seed: Mapping[str, Any], services: PlanServices) -> Pause:
    """Start a reviewed run and hand back the first thing to look at."""
    graph.invoke(dict(seed), config, context=services)
    return _pause_from(graph, config, _specs(None))


def where(graph, config: dict, pipeline: Sequence[NodeSpec] | None = None) -> Pause:
    """The current pause, without moving anything."""
    return _pause_from(graph, config, _specs(pipeline))


def approve(graph, config: dict, services: PlanServices) -> Pause:
    """Accept the paused stage and run on to the next one."""
    pause = _pause_from(graph, config, _specs(None))
    if pause.finished:
        raise ReviewError("the run is complete — there is nothing left to approve")
    graph.invoke(None, config, context=services)
    return _pause_from(graph, config, _specs(None))


def edit(graph, config: dict, services: PlanServices, **changes: Any) -> Pause:
    """Amend what the paused node produced, then run on.

    Written ``as_node`` the node under review, so the graph sees the edited
    values as that node's output and continues to its successors — the
    clinician's judgement enters the run in the same place the model's did,
    rather than being layered on top of it.

    Dropping an item is an edit, not a rejection: the stage stood, one entry
    did not. Rejection is for when the stage itself was wrong.
    """
    pause = _pause_from(graph, config, _specs(None))
    if not pause.started:
        raise ReviewError("nothing has run yet — there is nothing to edit")
    if pause.node is None:
        raise ReviewError("no paused node to attribute the edit to")

    declared = set(CarePlanState.__annotations__)
    unknown = sorted(set(changes) - declared)
    if unknown:
        # LangGraph drops undeclared keys in silence — the lesson from
        # `careplan-state-and-graph.md` §9. An edit that vanishes is worse
        # than one that fails, because the reviewer believes it took.
        raise ReviewError(
            f"not part of plan state: {', '.join(unknown)}. "
            f"An undeclared key would be dropped without a word."
        )

    graph.update_state(config, dict(changes), as_node=pause.node)
    graph.invoke(None, config, context=services)
    return _pause_from(graph, config, _specs(None))


def reject(graph, config: dict, services: PlanServices, *, feedback: str) -> Pause:
    """Send the paused stage back to be done again, with a reason.

    Re-runs the node that produced it. Everything after it is discarded by
    ``resume_at``'s invalidation — which here is mostly moot and entirely
    correct: in a reviewed run the later stages have not been computed yet,
    because the graph stopped before them.

    ``feedback`` is required. A rejection with no reason gives the node
    nothing to do differently, and it will propose the same thing again.
    """
    if not feedback.strip():
        raise ReviewError(
            "a rejection needs a reason — without one the node has nothing to "
            "do differently and will propose the same stage again"
        )
    pause = _pause_from(graph, config, _specs(None))
    if pause.node is None:
        raise ReviewError("nothing has run yet — there is nothing to reject")

    resume_at(graph, config, pause.node, services, feedback=feedback)
    return _pause_from(graph, config, _specs(None))


def run_to_end(graph, config: dict, services: PlanServices, *, limit: int = 20) -> Pause:
    """Approve every remaining stage. For tests and unattended replays.

    ``limit`` is a runaway guard rather than a policy: a reviewed run has as
    many pauses as it has model nodes, and if this loop exceeds that
    something is wrong with the position logic, not with the plan.
    """
    pause = _pause_from(graph, config, _specs(None))
    for _ in range(limit):
        if pause.finished:
            return pause
        pause = approve(graph, config, services)
    raise ReviewError(f"still pausing after {limit} approvals — the run is not advancing")


def summarise(pause: Pause) -> list[str]:
    """The pause as lines a person can read. Rendering lives here so the
    agent tools and the CLI cannot describe the same state differently."""
    if not pause.started:
        return ["nothing has run yet"]

    lines = [f"paused after {pause.node}"]
    lines.append(f"next: {pause.next_node}" if pause.next_node else "the plan is complete")

    for key, value in pause.proposed.items():
        if key.startswith("dropped_"):
            continue
        # Not every channel holds a list of drafts. `reconcile` writes a
        # ReconcileReport, and counting it raised a TypeError at the one
        # pause a clinician reaches last — the end of a finished plan.
        if not isinstance(value, list | tuple):
            if value is not None:
                lines.append(f"  {key}: {value}")
            continue
        items = value
        lines.append(f"  {key}: {len(items)}")
        for index, item in enumerate(items, 1):
            statement = getattr(item, "statement", None) or str(item)
            # `evidence_refs` is the field every draft carries. Shown even
            # when empty, and named as empty: an item citing nothing is the
            # single most important thing on a traceability-governed plan,
            # and omitting the line would make it the least visible.
            refs = ", ".join(getattr(item, "evidence_refs", ()) or ()) or "nothing"
            lines.append(f"    {index}. {statement}   [cites {refs}]")

    for key, value in pause.withheld.items():
        for entry in value or []:
            lines.append(f"  {key}: {entry}")
    return lines
