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
from hdh.modules.careplan.generate import (
    PlanDraft,
    Selector,
    propose_concerns,
    propose_goals,
    propose_interventions,
)
from hdh.modules.careplan.stratify import RiskFlag, stratify


@dataclass
class PlanResult:
    """What a generation run produced, and what it refused."""

    plan_id: int | None
    context: CarePlanContext
    flags: list[RiskFlag]
    draft: PlanDraft
    report: ValidationReport

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
    store=None,
    selector: Selector | None = None,
    dry_run: bool = False,
) -> PlanResult:
    """Nodes 1-5 and 7, then validation.

    Writes nothing when the draft has no concerns. An empty plan recorded
    as a plan is worse than no plan: it reports that the patient was
    assessed and nothing was found, when what happened is that nothing
    could be supported.
    """
    if store is None:
        from hdh.modules.careplan.knowledge import PgStore

        store = PgStore(session)
    if selector is None:
        from hdh.modules.careplan.generate import llm_selector

        selector = llm_selector()

    context = build_context(session, patient)
    flags = stratify(context)

    draft = PlanDraft()
    concerns, dropped = propose_concerns(store, context, flags, selector)
    draft.concerns.extend(concerns)
    draft.dropped.extend(dropped)

    goals, dropped = propose_goals(store, context, draft.concerns, selector)
    draft.goals.extend(goals)
    draft.dropped.extend(dropped)

    interventions, dropped = propose_interventions(store, context, draft.goals, selector)
    draft.interventions.extend(interventions)
    draft.dropped.extend(dropped)

    if not draft.concerns or dry_run:
        report = ValidationReport()
        if not draft.concerns:
            report.errors.append("nothing retrievable supported a concern — no plan written")
        return PlanResult(None, context, flags, draft, report)

    plan_id = assemble(session, patient, draft, _title(context, flags))
    report = validate(session, plan_id)
    if not report.ok:
        # Structural validation failing means the graph is wrong, and a
        # wrong graph must not persist just because the rows inserted.
        session.rollback()
        return PlanResult(None, context, flags, draft, report)
    session.commit()
    return PlanResult(plan_id, context, flags, draft, report)
