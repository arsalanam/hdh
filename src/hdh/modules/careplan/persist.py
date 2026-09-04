"""Turning a reviewed plan into a record, and keeping the trail.

A plan built through the agent lived only in a LangGraph checkpoint and,
optionally, an HTML file. Measured after a full review session: 14
checkpoints, and zero rows in `care_plan_records`, `health_concerns`,
`plan_goals` and `plan_interventions`.

That is not a small omission. Without a row there is no plan id, so
`careplan show` cannot display it and the FHIR endpoint has nothing to
serve; the approve/reject decision a clinician just made has nowhere to
live; `care_plan_records.prompt_set` — added precisely so a plan can say
what produced it — is never populated; and clearing the checkpoints
destroys the work.

`assemble()` already writes the graph. What was missing is the step that
calls it at the end of a review, the status that records what the reviewer
decided, and the audit events that say who changed what.

**Why the audit goes on `chart_audit_events` rather than a table of its
own.** It is the same question — *who changed this, when, and why* — and a
second trail would mean two places to look and two chances to look in the
wrong one. The entity column already carries the row's type, so a care plan
sits beside a condition without either needing to know about the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hdh.modules.careplan.generate import PlanDraft

#: Statuses a plan moves through. The enum is wider (`draft`,
#: `auto_evaluated`, `pending_review`); these are the ones the review path
#: actually produces.
AI_GENERATED = "ai_generated"
USER_EDITED = "user_edited"
APPROVED = "approved"
REJECTED = "rejected"

#: What the audit trail calls a care plan. Matches the entity naming the
#: chart tools use, so one query over `chart_audit_events` returns a
#: patient's whole history rather than most of it.
ENTITY = "CarePlan"


class PersistError(RuntimeError):
    """A plan could not be written, or a decision could not be recorded."""


@dataclass(frozen=True)
class Decision:
    """The outcome of a persist or approval attempt."""

    ok: bool
    plan_id: int | None
    detail: str

    def __bool__(self) -> bool:
        return self.ok


def _plans():
    from hdh.core.models import Base

    return Base.metadata.tables["care_plan_records"]


@dataclass(frozen=True)
class _Event:
    """One audit entry's payload, grouped so the writer keeps one job.

    Mirrors `chartedit._Change`, deliberately: the two write to the same
    table and a reader comparing them should not have to hold two shapes.
    """

    plan_id: int
    patient_id: int
    action: object
    reason: str
    before: dict | None = None
    after: dict | None = None


def _audit(session, event: _Event) -> None:
    """One event on the shared trail. Appended, never updated.

    ``action`` is the row-level vocabulary `AuditAction` already defines —
    create, amend, void. An approval is stored as an **amend**, because that
    is what happened to the row: its status field changed.

    The clinical meaning is not lost, and is not squeezed into the enum
    either. It lives in ``before``/``after`` as the status transition, and
    :func:`history` derives "approve" or "reject" from that when presenting
    the trail. Extending a PostgreSQL enum needs a migration on every
    deployed database — migration 0002 is the standing reminder of how that
    goes — and buys nothing a status transition does not already say.
    """
    from hdh.core.models import ChartAuditEvent

    session.add(
        ChartAuditEvent(
            actor_name="care-plan review",
            actor_source="agent",
            patient_id=event.patient_id,
            entity=ENTITY,
            row_id=event.plan_id,
            action=event.action,
            reason=event.reason,
            before=event.before or None,
            after=event.after or None,
        )
    )
    session.flush()


def persist_reviewed_plan(session, patient, values, thread_id: str = "", title: str = "") -> Decision:
    """Write a reviewed plan graph and record that a human shaped it.

    ``values`` is the plan state from the graph — the same mapping the
    review verbs read. Status is ``user_edited`` rather than
    ``ai_generated``: by the time a plan is persisted from a review, a
    clinician has approved or amended every stage, and recording it as
    machine output would misattribute their decisions.
    """
    from hdh.modules.careplan.graph import to_draft

    draft = to_draft(values)
    if not draft.concerns:
        return Decision(False, None, "nothing to write — the plan has no concerns")

    from hdh.modules.careplan.assemble import assemble

    plan_id = assemble(
        session,
        patient,
        draft,
        title or f"Care plan — {patient.mrn}",
        thread_id=thread_id,
    )
    session.execute(_plans().update().where(_plans().c.id == plan_id).values(status=USER_EDITED))
    from hdh.core.models import AuditAction

    _audit(
        session,
        _Event(
            plan_id=plan_id,
            patient_id=patient.id,
            action=AuditAction.CREATE,
            reason="written from a reviewed plan",
            after={
                "status": USER_EDITED,
                "concerns": len(draft.concerns),
                "goals": len(draft.goals),
                "interventions": len(draft.interventions),
                "deferred": list(draft.deferred),
            },
        ),
    )
    session.commit()
    return Decision(
        True,
        plan_id,
        f"plan #{plan_id}: {len(draft.concerns)} concerns, {len(draft.goals)} goals, "
        f"{len(draft.interventions)} interventions",
    )


def decide(session, plan_id: int, approved: bool, reason: str = "") -> Decision:
    """Record a clinician's approval or rejection of a written plan.

    A rejection **requires** a reason. An approval does not, but takes one:
    an approval nobody can audit is worth little more than a refusal nobody
    can check, and the next reader of this chart is entitled to know why a
    plan for a complex patient was signed off.

    Refuses to move a plan that is already decided, rather than overwriting
    it — a second decision means something upstream lost track, and silently
    allowing it would hide that. Amend the plan and write a new one instead.
    """
    from sqlalchemy import select

    if not approved and not reason.strip():
        return Decision(
            False,
            plan_id,
            "a rejection needs a reason — without one the record cannot say what was wrong",
        )

    plans = _plans()
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    if row is None:
        return Decision(False, None, f"no care plan #{plan_id}")
    if row.status in (APPROVED, REJECTED):
        return Decision(
            False,
            plan_id,
            f"plan #{plan_id} was already {row.status} — decide once, then amend and write a new plan",
        )

    new_status = APPROVED if approved else REJECTED
    session.execute(
        plans.update()
        .where(plans.c.id == plan_id)
        .values(
            status=new_status,
            rejection_reason=(reason.strip() or None) if not approved else None,
            updated_at=datetime.utcnow(),
        )
    )
    from hdh.core.models import AuditAction

    _audit(
        session,
        _Event(
            plan_id=plan_id,
            patient_id=row.patient_id,
            action=AuditAction.AMEND,
            reason=reason.strip() or "approved without comment",
            before={"status": row.status},
            after={"status": new_status},
        ),
    )
    session.commit()
    return Decision(True, plan_id, f"plan #{plan_id} {new_status}")


def history(session, plan_id: int) -> list[dict]:
    """Every recorded change to one plan, oldest first."""
    from sqlalchemy import select

    from hdh.core.models import ChartAuditEvent

    events = session.execute(
        select(ChartAuditEvent)
        .where(ChartAuditEvent.entity == ENTITY, ChartAuditEvent.row_id == plan_id)
        .order_by(ChartAuditEvent.occurred_at, ChartAuditEvent.id)
    ).scalars()
    return [
        {
            "when": event.occurred_at,
            "actor": event.actor_name,
            "source": event.actor_source,
            # The clinical action, derived from the status transition the
            # event already records. Stored as `amend` because that is what
            # happened to the row; presented as `approve`/`reject` because
            # that is what happened to the plan.
            "action": _presented_action(event),
            "reason": event.reason,
            "before": event.before,
            "after": event.after,
        }
        for event in events
    ]


def _presented_action(event) -> str:
    """What a reader of the trail should see."""
    after_status = (event.after or {}).get("status")
    before_status = (event.before or {}).get("status")
    if after_status in (APPROVED, REJECTED) and after_status != before_status:
        return "approve" if after_status == APPROVED else "reject"
    return getattr(event.action, "value", str(event.action))


def load_plan(session, plan_id: int) -> dict | None:
    """One saved plan and its whole graph, or None.

    Reading a plan back was the gap that made the agent lie about one. Asked
    to show a saved plan it had only `show_care_plan`, which reads the
    in-flight checkpoint, so it reported "no care plan in progress" as "no
    saved care plan exists" — with a persisted plan sitting in the table.
    """
    from sqlalchemy import select

    from hdh.core.models import Base

    plans = _plans()
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    if row is None:
        return None

    tables = Base.metadata.tables
    concerns = session.execute(
        select(tables["health_concerns"]).where(tables["health_concerns"].c.care_plan_id == plan_id)
    ).all()
    ids = [c.id for c in concerns] or [0]
    goals = session.execute(
        select(tables["plan_goals"]).where(tables["plan_goals"].c.concern_id.in_(ids))
    ).all()
    goal_ids = [g.id for g in goals] or [0]
    interventions = session.execute(
        select(tables["plan_interventions"]).where(tables["plan_interventions"].c.goal_id.in_(goal_ids))
    ).all()
    return {
        "row": row,
        "concerns": concerns,
        "goals": goals,
        "interventions": interventions,
        "superseded_by": superseded_by(session, plan_id),
        "in_force": current_plan_id(session, row.patient_id) == plan_id,
    }


def _standing(successor: int | None, in_force: bool) -> str:
    """Where a plan sits: in force, replaced, or neither.

    "Neither" is a real state and worth naming. A plan can have no successor
    and still not be the one to act on, because a later plan was saved
    independently rather than as an amendment of it.
    """
    if successor:
        return f"superseded by #{successor}"
    return "current" if in_force else "not in force"


def superseded_by(session, plan_id: int) -> int | None:
    """The plan that replaced this one, or None if it is still current.

    Derived rather than stored: a superseded plan keeps the status it was
    given, because it really was approved, and rewriting that field would
    destroy the fact the immutability exists to preserve.
    """
    from sqlalchemy import select

    plans = _plans()
    return session.execute(
        select(plans.c.id).where(plans.c.supersedes_id == plan_id).order_by(plans.c.id).limit(1)
    ).scalar()


def plans_for(session, patient_id: int) -> list[dict]:
    """Every saved plan for a patient, newest first, with its standing."""
    from sqlalchemy import select

    plans = _plans()
    rows = session.execute(
        select(plans).where(plans.c.patient_id == patient_id).order_by(plans.c.id.desc())
    ).all()
    successors = {row.supersedes_id: row.id for row in rows if row.supersedes_id}
    # Exactly one plan is in force: the newest that nothing replaced. An
    # earlier plan can also have no successor — plans are saved
    # independently, and only an amendment creates the link — so "has no
    # successor" would mark several of them current at once. A clinician
    # reading two current plans cannot tell which one to act on, which is
    # the same ambiguity this module exists to remove.
    in_force = next((row.id for row in rows if row.id not in successors), None)
    return [
        {
            "id": row.id,
            "title": row.title,
            "status": row.status,
            "created_at": row.created_at,
            "supersedes": row.supersedes_id,
            "superseded_by": successors.get(row.id),
            "current": row.id == in_force,
        }
        for row in rows
    ]


def current_plan_id(session, patient_id: int) -> int | None:
    """The plan in force: the newest one nothing has superseded.

    Not the same as :func:`latest_plan_id`, which is the newest row. They
    agree until a plan is superseded, and the question a clinician asks is
    always this one.
    """
    for plan in plans_for(session, patient_id):
        if plan["current"]:
            return int(plan["id"])
    return None


def _draft_from(plan: dict, keep: set[int]) -> PlanDraft:
    """A PlanDraft holding only the kept concerns and what hangs off them."""
    from hdh.modules.careplan.generate import (
        ConcernDraft,
        GoalDraft,
        InterventionDraft,
        PlanDraft,
    )

    def refs(row) -> tuple:
        raw = getattr(row, "evidence_refs", None) or {}
        return tuple(raw.get("chunks") or []) if isinstance(raw, dict) else ()

    concerns = [c for i, c in enumerate(plan["concerns"], start=1) if i in keep]
    kept_ids = {c.id for c in concerns}
    index = {c.id: i for i, c in enumerate(concerns)}
    goals = [g for g in plan["goals"] if g.concern_id in kept_ids]
    goal_index = {g.id: i for i, g in enumerate(goals)}
    interventions = [i for i in plan["interventions"] if i.goal_id in goal_index]
    return PlanDraft(
        concerns=[ConcernDraft(c.statement, str(c.concern_type), refs(c)) for c in concerns],
        goals=[GoalDraft(g.statement, index[g.concern_id], g.target_value or "", refs(g)) for g in goals],
        interventions=[
            InterventionDraft(
                i.statement,
                goal_index[i.goal_id],
                str(i.intervention_type),
                i.owner_role or "",
                refs(i),
            )
            for i in interventions
        ],
        deferred=list((plan["row"].deferred or {}).get("problems") or []),
    )


def amend_plan(session, plan_id: int, keep: set[int], reason: str = "") -> Decision:
    """Narrow a saved plan to the concerns named, in place or by superseding.

    An undecided plan is edited in place. A decided one is **not touched**:
    the amendment becomes a new plan carrying `supersedes_id`, so the record
    of what was actually approved survives its own revision. :func:`decide`
    has always said "decide once, then amend and write a new plan" — this is
    that sentence made executable rather than left to the reader.
    """
    from sqlalchemy import delete, select

    from hdh.core.models import AuditAction, Base, Patient

    plan = load_plan(session, plan_id)
    if plan is None:
        return Decision(False, None, f"no care plan #{plan_id}")

    total = len(plan["concerns"])
    unknown = sorted(n for n in keep if not 1 <= n <= total)
    if unknown:
        listed = ", ".join(str(n) for n in unknown)
        return Decision(False, plan_id, f"plan #{plan_id} has {total} concerns — no {listed}")
    if not keep:
        return Decision(
            False,
            plan_id,
            "keeping nothing is not an amendment — reject the plan instead, which records why",
        )
    if len(keep) == total:
        return Decision(False, plan_id, f"plan #{plan_id} already has exactly those {total} concerns")

    row = plan["row"]
    dropped = total - len(keep)
    if row.status in (APPROVED, REJECTED):
        successor = superseded_by(session, plan_id)
        if successor:
            return Decision(
                False,
                plan_id,
                f"plan #{plan_id} was already superseded by #{successor} — amend that one",
            )
        from hdh.modules.careplan.assemble import assemble

        patient = session.get(Patient, row.patient_id)
        new_id = assemble(session, patient, _draft_from(plan, keep), row.title)
        session.execute(
            _plans().update().where(_plans().c.id == new_id).values(status=USER_EDITED, supersedes_id=plan_id)
        )
        _audit(
            session,
            _Event(
                plan_id=new_id,
                patient_id=row.patient_id,
                action=AuditAction.CREATE,
                reason=reason.strip() or f"amended from #{plan_id}",
                after={"status": USER_EDITED, "concerns": len(keep), "supersedes": plan_id},
            ),
        )
        _audit(
            session,
            _Event(
                plan_id=plan_id,
                patient_id=row.patient_id,
                action=AuditAction.AMEND,
                reason=reason.strip() or f"superseded by #{new_id}",
                before={"status": row.status, "concerns": total, "superseded_by": None},
                after={"status": row.status, "concerns": total, "superseded_by": new_id},
            ),
        )
        session.commit()
        return Decision(
            True,
            new_id,
            f"#{plan_id} is {row.status} and was not changed — wrote #{new_id} superseding it, "
            f"with {len(keep)} of {total} concerns",
        )

    tables = Base.metadata.tables
    drop_ids = [c.id for i, c in enumerate(plan["concerns"], start=1) if i not in keep]
    goal_ids = [
        g.id
        for g in session.execute(
            select(tables["plan_goals"]).where(tables["plan_goals"].c.concern_id.in_(drop_ids))
        ).all()
    ] or [0]
    session.execute(
        delete(tables["plan_interventions"]).where(tables["plan_interventions"].c.goal_id.in_(goal_ids))
    )
    session.execute(delete(tables["plan_goals"]).where(tables["plan_goals"].c.concern_id.in_(drop_ids)))
    session.execute(delete(tables["health_concerns"]).where(tables["health_concerns"].c.id.in_(drop_ids)))
    session.execute(_plans().update().where(_plans().c.id == plan_id).values(updated_at=datetime.utcnow()))
    _audit(
        session,
        _Event(
            plan_id=plan_id,
            patient_id=row.patient_id,
            action=AuditAction.AMEND,
            reason=reason.strip() or f"dropped {dropped} concern(s) on review",
            before={"status": row.status, "concerns": total},
            after={"status": row.status, "concerns": len(keep)},
        ),
    )
    session.commit()
    return Decision(True, plan_id, f"plan #{plan_id} amended: {len(keep)} of {total} concerns kept")


def render_record(plan: dict) -> str:
    """A saved plan as text, with what every element traces to.

    Shared by the agent tool and `careplan show` so the two cannot drift
    into describing the same row differently.
    """
    row = plan["row"]
    standing = _standing(plan["superseded_by"], plan["in_force"])
    lines = [f"care plan #{row.id} — {row.title}", f"  status: {row.status} ({standing})"]
    if row.supersedes_id:
        lines.append(f"  supersedes: #{row.supersedes_id}")
    if row.rejection_reason:
        lines.append(f"  rejected because: {row.rejection_reason}")
    if row.prompt_set:
        lines.append(f"  prompts: {row.prompt_set}")
    for item in (row.deferred or {}).get("problems") or []:
        lines.append(f"  deferred by triage: {item}")

    goals_by_concern: dict = {}
    for goal in plan["goals"]:
        goals_by_concern.setdefault(goal.concern_id, []).append(goal)
    interventions_by_goal: dict = {}
    for item in plan["interventions"]:
        interventions_by_goal.setdefault(item.goal_id, []).append(item)

    def cites(record) -> str:
        raw = getattr(record, "evidence_refs", None) or {}
        chunks = raw.get("chunks") or [] if isinstance(raw, dict) else []
        return ", ".join(chunks) or "NOTHING"

    for number, concern in enumerate(plan["concerns"], start=1):
        lines.append("")
        lines.append(f"  {number}. [{concern.concern_type}] {concern.statement}   (cites {cites(concern)})")
        for goal in goals_by_concern.get(concern.id, []):
            target = f" -> {goal.target_value}" if goal.target_value else ""
            lines.append(f"        goal: {goal.statement}{target}   (cites {cites(goal)})")
            for item in interventions_by_goal.get(goal.id, []):
                owner = f" [{item.owner_role}]" if item.owner_role else ""
                lines.append(
                    f"            {item.intervention_type}: {item.statement}{owner}   (cites {cites(item)})"
                )
    if not plan["concerns"]:
        lines.append("  (no concerns on this plan)")
    return "\n".join(lines)


def latest_plan_id(session, patient_id: int) -> int | None:
    """The most recent plan for a patient, or None."""
    from sqlalchemy import select

    plans = _plans()
    return session.execute(
        select(plans.c.id).where(plans.c.patient_id == patient_id).order_by(plans.c.id.desc()).limit(1)
    ).scalar()


def as_of(session) -> date:
    """The chart's own reference date, so plan dates match the chart's."""
    from hdh.modules.caregaps import reference_date

    return reference_date(session)
