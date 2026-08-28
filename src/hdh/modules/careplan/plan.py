"""The orchestrator: chart in, plan graph out.

Design §7's nodes in order, with the store and the selector injected. That
injection is what lets the whole thing run in pytest with no PostgreSQL and
no API key — the same arrangement `tests/test_pipeline.py` already uses for
the agent graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from hdh.modules.careplan.assemble import ValidationReport, assemble, validate
from hdh.modules.careplan.context import CarePlanContext, build_context
from hdh.modules.careplan.evaluate import Evaluation
from hdh.modules.careplan.generate import (
    PlanDraft,
)
from hdh.modules.careplan.graph import (
    CarePlanState,
    compile_pipeline,
    node_index,
    run_from,
    thread_config,
    to_draft,
)
from hdh.modules.careplan.graph import PlanServices as GraphServices
from hdh.modules.careplan.reconcile import ReconcileReport
from hdh.modules.careplan.revise import RevisionLog
from hdh.modules.careplan.rubric import Rubric
from hdh.modules.careplan.stratify import RiskFlag, stratify
from hdh.modules.careplan.triage import deferral_lines, triage

#: The collaborators a plan run depends on.
#:
#: Re-exported from :mod:`~hdh.modules.careplan.graph` rather than defined
#: again here. There were briefly two of these — the nodes took one shape and
#: the orchestrator another — and they drifted the moment a field was added to
#: one of them. A collaborator bundle with two definitions is not a bundle.
PlanServices = GraphServices


@dataclass
class PlanResult:
    """What a generation run produced, and what it refused."""

    plan_id: int | None
    context: CarePlanContext
    flags: list[RiskFlag]
    draft: PlanDraft
    report: ValidationReport
    reconciliation: ReconcileReport | None = None
    rubric: Rubric | None = None
    evaluation: Evaluation | None = None
    evaluation_id: int | None = None
    revision: RevisionLog | None = None

    @property
    def refused(self) -> bool:
        """True when nothing was written — which is a legitimate outcome."""
        return self.plan_id is None


def _title(context: CarePlanContext, flags: list[RiskFlag]) -> str:
    lead = flags[0].statement if flags else "Care plan"
    return f"{lead} — {context.mrn}"


def generate_plan(
    session,
    patient,
    *,
    services: PlanServices | None = None,
    revise: bool = False,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> PlanResult:
    """Nodes 1-5 and 7, then validation.

    Writes nothing when the draft has no concerns. An empty plan recorded
    as a plan is worse than no plan: it reports that the patient was
    assessed and nothing was found, when what happened is that nothing
    could be supported.
    """
    services = (services or PlanServices()).resolved(session)
    store, selector, grader = services.store, services.selecting, services.grader

    # A run is durable when it has both a checkpointer and a thread to keep
    # its state under. One without the other is not half-durable, it is
    # neither, so they are decided together.
    checkpointer = services.checkpointer
    if checkpointer is not None and thread_id is None:
        thread_id = f"careplan-{patient.mrn}-{uuid4().hex[:12]}"
    if checkpointer is None:
        thread_id = None

    context = build_context(session, patient)
    flags = stratify(context)

    # Node 2b: decide what this plan is about before anything retrieves.
    # Without it, node 3 asked the corpus one question about ten problems
    # and got six weak answers back (#104).
    topics, deferred = triage(context, flags)

    deferrals = deferral_lines(deferred)
    reconciliation: ReconcileReport | None = None
    revision = None
    graded_rubric = None

    if revise and grader is not None:
        # M3c. Generate, grade and send back — all before anything is
        # written, so a round that scores worse is discarded rather than
        # persisted and deleted.
        from hdh.modules.careplan.revise import PlanInputs, revise_plan

        graded_rubric, revision = revise_plan(
            PlanInputs(
                store=store,
                context=context,
                flags=tuple(flags),
                topics=tuple(topics),
                selector=selector,
                deferred=tuple(deferrals),
                thread_id=thread_id or "",
                checkpointer=checkpointer,
            ),
            grader,
        )
        best = revision.best
        draft, reconciliation = best.draft, best.reconciliation
    else:
        # Nodes 3-6 through the declared pipeline. Node 6 still runs before
        # anything is written: reconciling after assembly would mean writing
        # rows only to delete them, and an audit trail that records a plan
        # proposing what it also forbade.
        seed: CarePlanState = {
            "context": context,
            "flags": flags,
            "topics": topics,
            "deferred": deferrals,
        }
        node_services = GraphServices(store=store, selector=selector)
        if checkpointer is not None and thread_id:
            graph = compile_pipeline(checkpointer)
            state = graph.invoke(seed, thread_config(thread_id), context=node_services)
        else:
            state = run_from(seed, node_services, node_index("concerns"))
        draft = to_draft(state)
        reconciliation = state.get("reconciliation")

    if not draft.concerns or dry_run:
        report = ValidationReport()
        if not draft.concerns:
            report.errors.append("nothing retrievable supported a concern — no plan written")
        return PlanResult(None, context, flags, draft, report, reconciliation)

    plan_id = assemble(session, patient, draft, _title(context, flags), thread_id or "")
    report = validate(session, plan_id)
    if not report.ok:
        # Structural validation failing means the graph is wrong, and a
        # wrong graph must not persist just because the rows inserted.
        session.rollback()
        return PlanResult(None, context, flags, draft, report, reconciliation)

    result = PlanResult(plan_id, context, flags, draft, report, reconciliation)
    result.revision = revision
    if revision is not None and graded_rubric is not None:
        # Already graded, as a draft. Re-grading the written rows would ask
        # the model the same question twice and could answer it differently.
        from hdh.modules.careplan.evaluate import record_evaluation

        result.rubric = graded_rubric
        result.evaluation = revision.best.evaluation
        result.evaluation_id = record_evaluation(session, plan_id, graded_rubric, revision.best.evaluation)
    elif grader is not None:
        # Design §9. Evaluation reads the plan back out of the database
        # rather than scoring the draft: what a reviewer will see is the
        # written graph, and that is what should be graded.
        from hdh.modules.careplan.evaluate import evaluate, record_evaluation
        from hdh.modules.careplan.facts import gather

        evidence = gather(session, plan_id, context, flags, reconciliation=reconciliation)
        rubric, evaluation = evaluate(evidence, grader)
        result.rubric = rubric
        result.evaluation = evaluation
        result.evaluation_id = record_evaluation(session, plan_id, rubric, evaluation)

    session.commit()
    return result
