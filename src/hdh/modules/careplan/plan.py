"""The orchestrator: chart in, plan graph out.

Design §7's nodes in order, with the store and the selector injected. That
injection is what lets the whole thing run in pytest with no PostgreSQL and
no API key — the same arrangement `tests/test_pipeline.py` already uses for
the agent graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from hdh.modules.careplan.assemble import ValidationReport, assemble, validate
from hdh.modules.careplan.context import CarePlanContext, build_context
from hdh.modules.careplan.evaluate import Evaluation, Grader
from hdh.modules.careplan.generate import (
    PlanDraft,
    Selector,
    propose_concerns,
    propose_goals,
    propose_interventions,
)
from hdh.modules.careplan.reconcile import ReconcileReport, reconcile
from hdh.modules.careplan.revise import RevisionLog
from hdh.modules.careplan.rubric import Rubric
from hdh.modules.careplan.stratify import RiskFlag, stratify
from hdh.modules.careplan.triage import deferral_lines, triage


@dataclass(frozen=True)
class PlanServices:
    """The collaborators a plan run depends on, injected together.

    ``store`` and ``selector`` default to their live implementations when
    omitted; ``grader`` stays None unless grading is wanted, because it
    costs a call per dimension. Bundled rather than passed one by one so
    that adding a fourth collaborator is a field rather than another
    parameter on every caller.
    """

    store: object | None = None
    selector: Selector | None = None
    grader: Grader | None = None

    def resolved(self, session) -> tuple[object, Selector, Grader | None]:
        """Store, selector and grader, with the live defaults filled in.

        Returns a tuple rather than another ``PlanServices`` so the two
        that are now guaranteed present are typed as present — a caller
        should not have to re-check what resolution just established.
        """
        store, selector = self.store, self.selector
        if store is None:
            from hdh.modules.careplan.knowledge import PgStore

            store = PgStore(session)
        if selector is None:
            from hdh.modules.careplan.generate import llm_selector

            selector = llm_selector()
        return store, selector, self.grader


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
) -> PlanResult:
    """Nodes 1-5 and 7, then validation.

    Writes nothing when the draft has no concerns. An empty plan recorded
    as a plan is worse than no plan: it reports that the patient was
    assessed and nothing was found, when what happened is that nothing
    could be supported.
    """
    store, selector, grader = (services or PlanServices()).resolved(session)

    context = build_context(session, patient)
    flags = stratify(context)

    # Node 2b: decide what this plan is about before anything retrieves.
    # Without it, node 3 asked the corpus one question about ten problems
    # and got six weak answers back (#104).
    topics, deferred = triage(context, flags)

    deferrals = deferral_lines(deferred)
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
            ),
            grader,
        )
        best = revision.best
        draft, reconciliation = best.draft, best.reconciliation
    else:
        draft = PlanDraft(deferred=deferrals)
        concerns, dropped = propose_concerns(store, context, flags, selector, topics)
        draft.concerns.extend(concerns)
        draft.dropped.extend(dropped)

        goals, dropped = propose_goals(store, context, draft.concerns, selector)
        draft.goals.extend(goals)
        draft.dropped.extend(dropped)

        interventions, dropped = propose_interventions(store, context, draft.goals, selector)
        draft.dropped.extend(dropped)

        # Node 6, before anything is written. Reconciling after assembly
        # would mean writing rows only to delete them, and an audit trail
        # that records a plan proposing what it also forbade.
        kept, reconciliation = reconcile(interventions, flags, goal_count=len(draft.goals))
        draft.interventions.extend(kept)

    if not draft.concerns or dry_run:
        report = ValidationReport()
        if not draft.concerns:
            report.errors.append("nothing retrievable supported a concern — no plan written")
        return PlanResult(None, context, flags, draft, report, reconciliation)

    plan_id = assemble(session, patient, draft, _title(context, flags))
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
