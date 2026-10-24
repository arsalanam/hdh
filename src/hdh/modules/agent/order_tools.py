"""Phase 4 of the media service (design media-io-service.md §11): the agent
drafts order changes from a recorded encounter.

`use_last_dictation` (phase 2) hands the agent the transcript; `apply_note`
charts the history and, from the note's PLAN, drafts the orders in it. These
tools let the agent then REFINE those drafts — add one the note missed, fix a
sig, withdraw one that should not stand — before a human reviews and releases
them. Together they are the "update the chart now" workflow, end to end, as
drafts.

Every order the agent touches is an **unreleased DRAFT** (`ServiceRequest`,
status=DRAFT, origin=AGENT). That is the whole of the relaxation, and it is
narrow by design:

- #121 holds — the agent *places requests*, it does not write the chart.
  Releasing a draft to Pharmacy stays the human act (`hdh orders release`), so
  nothing here sets a status to ACTIVE or writes an outbox bundle.
- `draft_order` creates a DRAFT, never ACTIVE.
- `update_order` / `cancel_order` act ONLY on a DRAFT that has not been
  released. An ACTIVE order (already sent to a lab or pharmacy) is a clinician's
  to change or stop — the tool refuses and says so rather than mutating a live
  order behind the fulfiller's back.
- A lab may be *ordered*, never *resulted* — a result value is partner-reported
  via `interchange`, never minted from a transcript (design §12).

Order changes from a transcript are drafts under review, never auto-committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class _OrderDraft:
    """The fields the agent dictates for a new order — grouped so the tool and
    its helper stay single-purpose. Route lives in the sig as a pharmacy reads
    it; quantity and refills are release-time details a human sets."""

    mrn: str
    kind: str
    display: str
    sig: str
    occurrence: str
    reason: str


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    return session.query(Patient).filter(Patient.mrn == mrn).first()


def _value(field) -> str:
    """Enum column or plain string, whichever the backend gave us."""
    return field.value if hasattr(field, "value") else str(field)


def _latest_visit_id(session, patient_id: int):
    """The patient's most recent encounter — where a new draft attaches so a
    result returning against it has somewhere to live (mirrors `hdh orders add`)."""
    from hdh.core.models import Visit

    visit = (
        session.query(Visit)
        .filter(Visit.patient_id == patient_id)
        .order_by(Visit.visit_date.desc(), Visit.id.desc())
        .first()
    )
    return visit.id if visit else None


def _order_permission(kind) -> str:
    """Drafting or editing a medication order is a prescribing act; a lab,
    procedure, referral or follow-up is a general charting act (mirrors the
    split `hdh orders` enforces)."""
    from hdh.core.models import ServiceKind

    return "medication:create" if kind is ServiceKind.MEDICATION else "chart:create"


def _actor_or_refusal(session, identity, permission: str):
    """The Actor to attribute a write to, or a refusal string if the signed-in
    identity lacks the permission. ``identity is None`` is a system/test context
    — attributed to the agent, no permission gate (mirrors refill_tools)."""
    from hdh.core.chartedit.contracts import Actor
    from hdh.core.models import EditSource

    if identity is None:
        return Actor("agent", EditSource.AGENT), None
    from hdh.core.identity import resolve_actor
    from hdh.core.identity.permissions import NotAuthenticated, Unauthorized, require

    try:
        require(identity, permission)
    except (Unauthorized, NotAuthenticated) as refusal:
        return None, str(refusal)
    return resolve_actor(session, identity, EditSource.AGENT), None


def _load_draft(session, order_id: int):
    """One editable order, or a refusal string. The agent may refine only an
    UNRELEASED draft. A released (ACTIVE) or otherwise non-draft order is a
    clinician's to change — never mutated here."""
    from hdh.core.models import RequestStatus, ServiceRequest

    order = session.get(ServiceRequest, order_id)
    if order is None or order.voided_at is not None:
        return None, f"no draft order #{order_id}"
    if order.status is not RequestStatus.DRAFT:
        return None, (
            f"order #{order_id} ({order.display}) is {_value(order.status)}, not a draft — "
            "a released order is changed or stopped by a clinician, not here"
        )
    return order, None


def _render_orders(session, mrn: str) -> str:
    """The patient's outstanding (draft + active) orders, with ids to target."""
    from hdh.core.models import RequestStatus, ServiceRequest

    patient = _patient(session, mrn)
    if patient is None:
        return f"no patient {mrn}"
    rows = (
        session.query(ServiceRequest)
        .filter(
            ServiceRequest.patient_id == patient.id,
            ServiceRequest.voided_at.is_(None),
            ServiceRequest.status.in_((RequestStatus.DRAFT, RequestStatus.ACTIVE)),
        )
        .order_by(ServiceRequest.status, ServiceRequest.requested_date.desc(), ServiceRequest.id)
        .all()
    )
    if not rows:
        return f"{mrn} has no outstanding orders"
    lines = [f"outstanding orders for {mrn} ({len(rows)}):"]
    for r in rows:
        code = f"{r.code_system}:{r.code}" if r.code else "uncoded"
        sig = f" — {r.sig}" if r.sig else ""
        lines.append(f"  #{r.id} [{_value(r.status)}] {_value(r.kind)}: {r.display} ({code}){sig}")
    return "\n".join(lines)


def _create_draft(session, identity, d: _OrderDraft) -> str:
    """Validate and insert one DRAFT ServiceRequest(origin=AGENT)."""
    from hdh.core.chartedit import record_creation
    from hdh.core.models import RequestOrigin, RequestStatus, ServiceKind, ServiceRequest

    patient = _patient(session, d.mrn)
    if patient is None:
        return f"no patient {d.mrn}"
    try:
        order_kind = ServiceKind(d.kind.strip().lower())
    except ValueError:
        return f"unknown order kind {d.kind!r} — expected one of: {' | '.join(m.value for m in ServiceKind)}"
    occurrence_date = None
    if d.occurrence.strip():
        try:
            occurrence_date = date.fromisoformat(d.occurrence.strip())
        except ValueError:
            return f"occurrence must be a date (YYYY-MM-DD), got {d.occurrence!r}"

    actor, refusal = _actor_or_refusal(session, identity, _order_permission(order_kind))
    if refusal is not None:
        return refusal

    order = ServiceRequest(
        patient_id=patient.id,
        visit_id=_latest_visit_id(session, patient.id),
        kind=order_kind,
        status=RequestStatus.DRAFT,  # never ACTIVE — releasing is the human act
        origin=RequestOrigin.AGENT,  # permanent: an agent-placed request stays marked as one
        display=d.display,
        requested_date=date.today(),
        occurrence_date=occurrence_date,
        sig=d.sig or None,
    )
    session.add(order)
    session.flush()  # the audit event needs the row id
    record_creation(
        session, actor, "ServiceRequest", order, reason=d.reason or "drafted by the agent from an encounter"
    )
    session.commit()
    return json.dumps(
        {
            "drafted_order_id": order.id,
            "kind": _value(order_kind),
            "status": "draft",
            "display": d.display,
            "mrn": d.mrn,
            "note": "draft only — a clinician releases it with `hdh orders release`",
        },
        indent=1,
    )


def _apply_amend(session, identity, order_id: int, changes: dict, reason: str) -> str:
    """Amend an unreleased draft through the audited chart-edit path."""
    from hdh.core.chartedit import EditAction, apply_edits
    from hdh.core.chartedit.contracts import ChartEdit

    order, refusal = _load_draft(session, order_id)
    if refusal is not None:
        return refusal
    if not changes:
        return "nothing to change — pass at least one field to update"
    actor, perm_refusal = _actor_or_refusal(session, identity, _order_permission(order.kind))
    if perm_refusal is not None:
        return perm_refusal
    outcomes = apply_edits(
        session, actor, [ChartEdit("ServiceRequest", order_id, EditAction.AMEND, changes, reason)]
    )
    return outcomes[0].detail


def _apply_void(session, identity, order_id: int, reason: str) -> str:
    """Withdraw (void) an unreleased draft through the audited chart-edit path."""
    from hdh.core.chartedit import EditAction, apply_edits
    from hdh.core.chartedit.contracts import ChartEdit

    order, refusal = _load_draft(session, order_id)
    if refusal is not None:
        return refusal
    actor, perm_refusal = _actor_or_refusal(session, identity, _order_permission(order.kind))
    if perm_refusal is not None:
        return perm_refusal
    outcomes = apply_edits(
        session, actor, [ChartEdit("ServiceRequest", order_id, EditAction.VOID, {}, reason)]
    )
    return outcomes[0].detail


def _amend_changes(display: str, sig: str, occurrence: str) -> dict | str:
    """The non-empty fields to amend, or an error string for a bad date."""
    changes: dict[str, object] = {}
    if display.strip():
        changes["display"] = display.strip()
    if sig.strip():
        changes["sig"] = sig.strip()
    if occurrence.strip():
        try:
            changes["occurrence_date"] = date.fromisoformat(occurrence.strip())
        except ValueError:
            return f"occurrence must be a date (YYYY-MM-DD), got {occurrence!r}"
    return changes


def build_order_tools(session, *, identity=None) -> list:
    """The agent's order-drafting toolset (phase 4). Needs a session to read and
    write; absent one it offers nothing, so the always-on baseline is unchanged."""
    try:
        from anthropic import beta_tool
    except ImportError:
        return []

    from hdh.core.models import tool_guard

    if session is None:
        return []

    guard = tool_guard(session)

    @beta_tool
    @guard
    def list_orders(mrn: str) -> str:
        """List a patient's outstanding orders (service requests) with their id, kind, status and what was ordered. Use this to see which drafts exist before refining them, or to find the order id for update_order / cancel_order. Draft orders are the ones you may change; an active order has been released and is a clinician's to alter.

        Args:
            mrn: The patient's medical record number.
        """
        return _render_orders(session, mrn)

    @beta_tool
    @guard
    def draft_order(
        mrn: str, kind: str, display: str, sig: str = "", occurrence: str = "", reason: str = ""
    ) -> str:
        """Draft a new order for a patient — a medication, lab, procedure, referral or follow-up — from what a provider asked for (e.g. in a recorded encounter). It is created as a DRAFT only: a clinician releases it to the pharmacy or lab with `hdh orders release`; you never release it. Put the route in the sig as a pharmacy reads it ("by mouth"); quantity and refills are set at release. Ordering a lab requests a test, it does NOT report a result. Report the drafted order's id back to the provider.

        Args:
            mrn: The patient's medical record number.
            kind: One of medication | lab | procedure | referral | follow_up.
            display: What is ordered, verbatim, e.g. "Basic metabolic panel" or "Amlodipine 5 mg daily".
            sig: For a medication, the directions a pharmacy reads, e.g. "1 tablet by mouth once daily". Optional.
            occurrence: When it should happen (YYYY-MM-DD), if stated. Optional.
            reason: Why it is being ordered, in a few words (recorded on the chart's audit trail).
        """
        return _create_draft(session, identity, _OrderDraft(mrn, kind, display, sig, occurrence, reason))

    @beta_tool
    @guard
    def update_order(
        order_id: int, reason: str, display: str = "", sig: str = "", occurrence: str = ""
    ) -> str:
        """Change a DRAFT order that has not been released yet — correct its description, directions (sig) or scheduled date. Only unreleased drafts can be changed here; a released (active) order is a clinician's to alter. A reason is required and is recorded on the chart's audit trail. Pass only the fields you are changing.

        Args:
            order_id: The order's id (from list_orders).
            reason: Why the change is being made (required, recorded on the audit trail).
            display: New description, if changing it. Optional.
            sig: New directions, if changing them. Optional.
            occurrence: New scheduled date (YYYY-MM-DD), if changing it. Optional.
        """
        changes = _amend_changes(display, sig, occurrence)
        if isinstance(changes, str):
            return changes
        return _apply_amend(session, identity, order_id, changes, reason)

    @beta_tool
    @guard
    def cancel_order(order_id: int, reason: str) -> str:
        """Withdraw a DRAFT order that has not been released yet — remove it from the pending orders, e.g. because it was dictated in error or superseded. Only unreleased drafts can be withdrawn here; stopping a released (active) order is a clinician's act. A reason is required and is recorded on the chart's audit trail.

        Args:
            order_id: The order's id (from list_orders).
            reason: Why the draft is being withdrawn (required, recorded on the audit trail).
        """
        return _apply_void(session, identity, order_id, reason)

    return [list_orders, draft_order, update_order, cancel_order]
