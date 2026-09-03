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
