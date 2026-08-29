"""Node 7 and the structural validation that follows it.

Design §7-§8. Writing the plan is deterministic: the drafts arrive already
checked against what was offered, and this turns them into rows whose
foreign keys make the graph unbreakable.

The validation afterwards is deliberately redundant with the schema. The
foreign keys already guarantee edge coverage, and it is asserted anyway —
because a guarantee nobody checks is a guarantee nobody notices losing,
and this is the invariant the whole design rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from hdh.modules.careplan.generate import PlanDraft

#: The status a plan carries when the model has proposed it and no human
#: has looked yet. Nothing downstream may treat this as approved.
AI_GENERATED = "ai_generated"


@dataclass
class ValidationReport:
    """What the structural checks found."""

    errors: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return (
        t["care_plan_records"],
        t["health_concerns"],
        t["plan_goals"],
        t["plan_interventions"],
    )


def assemble(session, patient, draft: PlanDraft, title: str, thread_id: str = "") -> int:
    """Write the plan graph; returns the care-plan id.

    Order is forced by the foreign keys — a concern cannot exist before its
    plan, a goal before its concern. That is the schema doing the work
    rather than a comment asking for it.
    """
    from sqlalchemy import insert

    from hdh.modules.careplan.prompts import prompt_set

    plans, concerns_t, goals_t, interventions_t = _tables()
    now = datetime.utcnow()

    plan_id = session.execute(
        insert(plans).returning(plans.c.id),
        [
            {
                "patient_id": patient.id,
                "title": title,
                "status": AI_GENERATED,
                # What triage set aside, written with the plan rather than
                # left in the run that produced it. A reviewer reading this
                # plan next month needs to know it chose not to address
                # three problems, not to infer it from an absence.
                "deferred": {"problems": list(draft.deferred)},
                # The thread this plan's checkpoints live under. Written so a
                # reviewer can resume the run that produced the plan — the
                # column has existed since milestone 1 and been empty since.
                "checkpoint_thread_id": thread_id or None,
                # Which wording produced this plan. Two plans for the same
                # patient from the same chart can differ entirely because
                # the prompt changed between them, and without this the
                # record cannot say which one it is.
                "prompt_set": prompt_set().stamp,
                "created_at": now,
                "updated_at": now,
            }
        ],
    ).scalar_one()

    concern_ids: list[int] = []
    for concern in draft.concerns:
        concern_ids.append(
            session.execute(
                insert(concerns_t).returning(concerns_t.c.id),
                [
                    {
                        "care_plan_id": plan_id,
                        "concern_type": concern.concern_type,
                        "statement": concern.statement,
                        "source": "ai",
                        "evidence_refs": {"chunks": list(concern.evidence_refs)},
                    }
                ],
            ).scalar_one()
        )

    goal_ids: list[int] = []
    for goal in draft.goals:
        goal_ids.append(
            session.execute(
                insert(goals_t).returning(goals_t.c.id),
                [
                    {
                        "care_plan_id": plan_id,
                        "concern_id": concern_ids[goal.concern_index],
                        "statement": goal.statement,
                        "target_value": goal.target_value or None,
                        "expressed_by": "clinician",
                        "status": "proposed",
                        "source": "ai",
                        "evidence_refs": {"chunks": list(goal.evidence_refs)},
                    }
                ],
            ).scalar_one()
        )

    for intervention in draft.interventions:
        session.execute(
            insert(interventions_t),
            [
                {
                    "care_plan_id": plan_id,
                    "goal_id": goal_ids[intervention.goal_index],
                    "intervention_type": intervention.intervention_type,
                    "statement": intervention.statement,
                    "owner_role": intervention.owner_role or None,
                    "source": "ai",
                    "evidence_refs": {"chunks": list(intervention.evidence_refs)},
                }
            ],
        )

    session.flush()
    return int(plan_id)


def validate(session, plan_id: int) -> ValidationReport:
    """Structural validation, deterministic (§7).

    Three things, none of them asked of the model:

    - every goal traces to a concern and every intervention to a goal
    - every AI-authored element carries evidence
    - the plan is not empty, because an empty plan that reports success is
      worse than one that says it found nothing
    """
    from sqlalchemy import select

    report = ValidationReport()
    _plans, concerns_t, goals_t, interventions_t = _tables()

    concerns = session.execute(select(concerns_t).where(concerns_t.c.care_plan_id == plan_id)).all()
    goals = session.execute(select(goals_t).where(goals_t.c.care_plan_id == plan_id)).all()
    interventions = session.execute(
        select(interventions_t).where(interventions_t.c.care_plan_id == plan_id)
    ).all()

    report.checked.append(
        f"{len(concerns)} concern(s), {len(goals)} goal(s), {len(interventions)} intervention(s)"
    )
    if not concerns:
        report.errors.append("plan has no concerns — nothing to build on")

    concern_ids = {row.id for row in concerns}
    for goal in goals:
        if goal.concern_id not in concern_ids:
            report.errors.append(f"goal {goal.id} points outside this plan's concerns")
    goal_ids = {row.id for row in goals}
    for intervention in interventions:
        if intervention.goal_id not in goal_ids:
            report.errors.append(f"intervention {intervention.id} points outside this plan's goals")
    report.checked.append("edge coverage")

    for label, rows in (("concern", concerns), ("goal", goals), ("intervention", interventions)):
        for row in rows:
            if row.source != "ai":
                continue
            refs = (row.evidence_refs or {}).get("chunks") or []
            if not refs:
                report.errors.append(f"{label} {row.id} was AI-proposed with no evidence")
    report.checked.append("evidence on every AI element")

    return report
