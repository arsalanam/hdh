"""The care plan, as FHIR sees it.

The schema hint on `CarePlanRecord` gives core a flat CarePlan — id, title,
description, subject. That is all a declared emitter can do, because it maps
one row to one resource and a care plan is four tables: the plan, its
concerns, the goals under each concern, and the interventions under each
goal.

This enricher adds the rest, and the mapping is the interesting part:

| hdh | FHIR | why |
|---|---|---|
| `status` | `status` + `intent` | `approved` is *active*; `rejected` is *revoked*; anything else is a *draft* a receiving system must not act on |
| concern | `addresses` | what the plan is about, as text — no Condition reference, because a concern is not a diagnosis row and pretending otherwise would invent a link |
| goal | `activity.detail.description` | contained Goal resources need ids that survive a round trip; the description carries the target so nothing is lost |
| intervention | `activity.detail` | with `kind` from the intervention type and the owner in `performer` text |
| deferred | `note` | a receiving system that cannot see what was set aside is reading a filtered plan, exactly as a human reviewer would be |
| `supersedes_id` | `replaces` | the plan this one revised |

**A superseded plan leaves as `revoked`, whatever it was.** hdh keeps its
real status because it really was approved; FHIR has no value for "replaced"
and the question a receiver is asking is only ever *may I act on this*. So
the export answers that question, and `replaces` on the successor carries
the rest.

**Citations travel as extensions.** Every element in an hdh plan cites the
document it came from, and dropping that on export would export the claim
without its support — which is the one thing this module refuses to do
anywhere else.
"""

from __future__ import annotations

from typing import Any, ClassVar

#: hdh status -> (FHIR status, whether a receiver may act on it).
#:
#: The FHIR value set is not ours to extend, so several hdh statuses share a
#: FHIR one. The distinction that must survive is actionable-or-not: a plan
#: nobody approved must not arrive looking active.
STATUS_MAP: dict[str, str] = {
    "approved": "active",
    "rejected": "revoked",
    "user_edited": "draft",
    "ai_generated": "draft",
    "auto_evaluated": "draft",
    "pending_review": "draft",
    "draft": "draft",
}

#: hdh intervention type -> FHIR CarePlan.activity.detail.kind.
KIND_MAP: dict[str, str] = {
    "medication": "MedicationRequest",
    "service": "ServiceRequest",
    "referral": "ServiceRequest",
    "monitoring": "ServiceRequest",
    "education": "ServiceRequest",
}

#: Where a citation rides. A plan element without its source is a claim
#: without support, and hdh does not emit those.
CITATION_URL = "https://github.com/arsalanam/hdh/fhir/StructureDefinition/evidence-ref"


class CarePlanEnricher:
    """Fills in what a flat row cannot carry."""

    resource_type: ClassVar[str] = "CarePlan"

    def enrich(self, resource: Any, entity: Any, ctx: Any) -> None:
        """Add status, addressed concerns, goals, activities and deferrals."""
        if entity is None:
            return
        from sqlalchemy.orm import object_session

        session = object_session(ctx.patient)
        if session is None:
            return

        # A superseded plan keeps its own status in hdh — it really was
        # approved, and rewriting that would destroy what the supersede
        # model exists to preserve. FHIR has no such value, and the
        # distinction that must survive the export is actionable-or-not, so
        # a replaced plan leaves as `revoked` however it was decided. The
        # link itself travels in `replaces`.
        from hdh.modules.careplan.persist import superseded_by

        successor = superseded_by(session, entity.id)
        resource.status = "revoked" if successor else STATUS_MAP.get(str(entity.status), "draft")
        if entity.supersedes_id:
            resource.replaces = [{"reference": f"CarePlan/{entity.supersedes_id}"}]
        rows = _plan_rows(session, entity.id)
        if not rows:
            return

        concerns, goals, interventions = rows
        resource.addresses = None  # concerns are text, not Condition references
        notes = [f"Concern: {c.statement}" for c in concerns]
        for deferred in (entity.deferred or {}).get("problems") or []:
            notes.append(f"Deferred by triage: {deferred}")
        if notes:
            _set_notes(resource, notes)

        activities = []
        by_concern = {c.id: c for c in concerns}
        for goal in goals:
            concern = by_concern.get(goal.concern_id)
            for intervention in [i for i in interventions if i.goal_id == goal.id]:
                activities.append(_activity(goal, intervention, concern))
        if activities:
            resource.activity = activities


def _plan_rows(session, plan_id: int):
    """Concerns, goals and interventions for one plan, or None."""
    from sqlalchemy import select

    from hdh.core.models import Base

    tables = Base.metadata.tables
    concerns = session.execute(
        select(tables["health_concerns"]).where(tables["health_concerns"].c.care_plan_id == plan_id)
    ).all()
    if not concerns:
        return None
    concern_ids = [c.id for c in concerns]
    goals = session.execute(
        select(tables["plan_goals"]).where(tables["plan_goals"].c.concern_id.in_(concern_ids))
    ).all()
    goal_ids = [g.id for g in goals] or [0]
    interventions = session.execute(
        select(tables["plan_interventions"]).where(tables["plan_interventions"].c.goal_id.in_(goal_ids))
    ).all()
    return concerns, goals, interventions


def _citations(row) -> list[str]:
    refs = getattr(row, "evidence_refs", None) or {}
    return list(refs.get("chunks") or []) if isinstance(refs, dict) else []


def _activity(goal, intervention, concern) -> Any:
    """One intervention, carrying the goal it serves and what it cites."""
    from fhir.resources.R4B.careplan import CarePlanActivity, CarePlanActivityDetail
    from fhir.resources.R4B.codeableconcept import CodeableConcept
    from fhir.resources.R4B.extension import Extension

    target = f" (target: {goal.target_value})" if getattr(goal, "target_value", "") else ""
    # A role is not a Practitioner reference, so the owner rides in the text
    # rather than in `performer` — inventing a reference to a practitioner
    # who was never named is the kind of confident approximation this
    # module refuses everywhere else.
    owner = f"\nOwner: {intervention.owner_role}" if getattr(intervention, "owner_role", "") else ""
    description = (
        f"{intervention.statement}\n"
        f"Goal: {goal.statement}{target}\n"
        f"Concern: {concern.statement if concern is not None else '—'}"
        f"{owner}"
    )
    detail = CarePlanActivityDetail(
        kind=KIND_MAP.get(str(getattr(intervention, "intervention_type", "")), "ServiceRequest"),
        status="not-started",
        description=description,
        code=CodeableConcept(text=goal.statement),
    )

    citations = _citations(intervention) + _citations(goal)
    activity = CarePlanActivity(detail=detail)
    if citations:
        activity.extension = [
            Extension(url=CITATION_URL, valueString=ref) for ref in dict.fromkeys(citations)
        ]
    return activity


def _set_notes(resource, notes: list[str]) -> None:
    from fhir.resources.R4B.annotation import Annotation

    existing = list(resource.note or [])
    resource.note = existing + [Annotation(text=note) for note in notes]


def fhir_enrichers() -> list[Any]:
    """The module's contribution to a FHIR export."""
    return [CarePlanEnricher()]
