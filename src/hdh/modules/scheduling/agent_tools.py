"""The agent's scheduling toolset — find, book, reschedule, cancel an
appointment, and link an encounter to the appointment it fulfilled.

This is the only control surface for scheduling: the web agent (``/ask``) and the
CLI agent reach these tools; there is no scheduling UI and no appointment CRUD CLI.
Structure mirrors ``agent/order_tools.py`` — thin ``@beta_tool @guard`` closures
over module-level helpers, audited writes via :mod:`hdh.core.chartedit`, and
refusals (not exceptions) the model can act on. Booking refuses on a double-booking
and flags out-of-hours slots; rescheduling/cancelling go through the audited edit path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from hdh.modules.scheduling import appointments as store

# Booking is a general charting/administrative act; changing or cancelling a
# booked slot is a chart edit. Reusing the coarse chart domain (permissions are
# coarse-by-domain by design) keeps the vocabulary unchanged for this phase.
_CREATE_PERM = "chart:create"
_EDIT_PERM = "chart:edit"
_DEFAULT_DURATION_MIN = 30


@dataclass(frozen=True)
class _Booking:
    """The fields the agent dictates for a new appointment — grouped so the
    helper stays single-purpose (mirrors ``order_tools._OrderDraft``)."""

    mrn: str
    provider: str
    start: str
    duration_minutes: int
    reason: str


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    return session.query(Patient).filter(Patient.mrn == mrn).first()


def _actor_or_refusal(session, identity, permission: str):
    """The Actor to attribute a write to, or a refusal string if the identity
    lacks the permission. ``identity is None`` is a system/test context."""
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


def _resolve_provider(session, text: str):
    """A provider by identifier (exact) or name (contains), or a refusal string.
    Ambiguity is refused with the candidates, never guessed."""
    from hdh.core.models import Provider

    text = (text or "").strip()
    if not text:
        return None, "name a provider (their name or identifier)"
    exact = session.query(Provider).filter(Provider.identifier == text).first()
    if exact is not None:
        return exact, None
    matches = session.query(Provider).filter(Provider.name.ilike(f"%{text}%")).order_by(Provider.name).all()
    if not matches:
        return None, f"no provider matching {text!r}"
    if len(matches) > 1:
        names = ", ".join(f"{p.name} ({p.identifier})" for p in matches[:8])
        return None, f"{text!r} matches several providers — which one? {names}"
    return matches[0], None


def _parse_dt(text: str):
    """An ISO-8601 datetime (``YYYY-MM-DD HH:MM`` or with a ``T``), or a refusal."""
    try:
        return datetime.fromisoformat((text or "").strip()), None
    except ValueError:
        return None, f"could not read a date/time from {text!r} — use ISO 8601, e.g. 2026-10-27 09:00"


def _load_active(session, appointment_id: int):
    """One live appointment to change, or a refusal string."""
    from hdh.core.models import Appointment

    appointment = session.get(Appointment, appointment_id)
    if appointment is None or appointment.voided_at is not None:
        return None, f"no appointment #{appointment_id}"
    if appointment.status in ("cancelled", "completed"):
        return None, f"appointment #{appointment_id} is {appointment.status} — it cannot be changed"
    return appointment, None


def _render(appointments) -> str:
    """A compact, agent-readable list of appointments with ids to target."""
    lines = []
    for a in appointments:
        who = a.provider.name if a.provider else f"provider {a.provider_id}"
        mrn = a.patient.mrn if a.patient else f"patient {a.patient_id}"
        when = f"{a.start_at:%Y-%m-%d %H:%M}–{a.end_at:%H:%M}"
        reason = f" — {a.reason}" if a.reason else ""
        lines.append(f"  #{a.id} [{a.status}] {when} · {who} · {mrn}{reason}")
    return "\n".join(lines)


def _amend(session, actor, appointment_id: int, changes: dict, reason: str) -> str:
    """Apply an audited amend to one appointment and return the outcome line."""
    from hdh.core.chartedit import EditAction, apply_edits
    from hdh.core.chartedit.contracts import ChartEdit

    outcomes = apply_edits(
        session, actor, [ChartEdit("Appointment", appointment_id, EditAction.AMEND, changes, reason)]
    )
    return outcomes[0].detail


def _find(session, mrn: str, provider: str, since: str, until: str) -> str:
    patient_id = None
    if mrn.strip():
        patient = _patient(session, mrn.strip())
        if patient is None:
            return f"no patient {mrn}"
        patient_id = patient.id
    provider_id = None
    if provider.strip():
        found, refusal = _resolve_provider(session, provider)
        if refusal is not None:
            return refusal
        provider_id = found.id
    if patient_id is None and provider_id is None:
        return "name a patient (MRN) or a provider to look up appointments"
    since_dt = _parse_dt(since)[0] if since.strip() else None
    until_dt = _parse_dt(until)[0] if until.strip() else None
    rows = store.find(session, patient_id=patient_id, provider_id=provider_id, since=since_dt, until=until_dt)
    return f"appointments ({len(rows)}):\n{_render(rows)}" if rows else "no appointments match"


def _book(session, identity, booking: _Booking) -> str:
    from hdh.core.chartedit import record_creation
    from hdh.core.models import primary_location_id

    patient = _patient(session, booking.mrn)
    if patient is None:
        return f"no patient {booking.mrn}"
    found, refusal = _resolve_provider(session, booking.provider)
    if refusal is not None:
        return refusal
    start_dt, when_refusal = _parse_dt(booking.start)
    if when_refusal is not None:
        return when_refusal
    end_dt = start_dt + timedelta(minutes=booking.duration_minutes or _DEFAULT_DURATION_MIN)
    clash = store.overlapping(session, found.id, start_dt, end_dt)
    if clash:
        return f"double-booking refused: {found.name} already has appointment #{clash[0].id} at {clash[0].start_at:%Y-%m-%d %H:%M}"
    actor, perm_refusal = _actor_or_refusal(session, identity, _CREATE_PERM)
    if perm_refusal is not None:
        return perm_refusal
    appointment = store.create(
        session,
        store.NewAppointment(
            patient_id=patient.id,
            provider_id=found.id,
            start_at=start_dt,
            end_at=end_dt,
            reason=booking.reason or None,
            location_id=primary_location_id(session, found.id),
            created_by_id=getattr(actor, "provider_id", None),
        ),
    )
    record_creation(
        session, actor, "Appointment", appointment, reason=booking.reason or "booked by the agent"
    )
    session.commit()
    payload = {
        "appointment_id": appointment.id,
        "patient": booking.mrn,
        "provider": found.name,
        "start": f"{start_dt:%Y-%m-%d %H:%M}",
        "end": f"{end_dt:%H:%M}",
        "status": "scheduled",
    }
    warning = store.clinic_hours_warning(session, found.id, start_dt, end_dt)
    if warning:
        payload["warning"] = warning
    return json.dumps(payload, indent=1)


def _reschedule(
    session, identity, appointment_id: int, start: str, reason: str, duration_minutes: int
) -> str:
    appointment, refusal = _load_active(session, appointment_id)
    if refusal is not None:
        return refusal
    start_dt, when_refusal = _parse_dt(start)
    if when_refusal is not None:
        return when_refusal
    current_min = int((appointment.end_at - appointment.start_at).total_seconds() // 60)
    end_dt = start_dt + timedelta(minutes=duration_minutes or current_min or _DEFAULT_DURATION_MIN)
    clash = store.overlapping(session, appointment.provider_id, start_dt, end_dt, exclude_id=appointment.id)
    if clash:
        return f"double-booking refused: slot overlaps appointment #{clash[0].id} at {clash[0].start_at:%Y-%m-%d %H:%M}"
    actor, perm_refusal = _actor_or_refusal(session, identity, _EDIT_PERM)
    if perm_refusal is not None:
        return perm_refusal
    return _amend(session, actor, appointment_id, {"start_at": start_dt, "end_at": end_dt}, reason)


def _cancel(session, identity, appointment_id: int, reason: str) -> str:
    _, refusal = _load_active(session, appointment_id)
    if refusal is not None:
        return refusal
    actor, perm_refusal = _actor_or_refusal(session, identity, _EDIT_PERM)
    if perm_refusal is not None:
        return perm_refusal
    changes = {"status": "cancelled", "cancelled_at": datetime.utcnow(), "cancel_reason": reason}
    return _amend(session, actor, appointment_id, changes, reason)


def _link(session, identity, visit_id: int, appointment_id: int, reason: str) -> str:
    from hdh.core.chartedit import EditAction, apply_edits
    from hdh.core.chartedit.contracts import ChartEdit

    _, refusal = _load_active(session, appointment_id)
    if refusal is not None:
        return refusal
    actor, perm_refusal = _actor_or_refusal(session, identity, _EDIT_PERM)
    if perm_refusal is not None:
        return perm_refusal
    edit = ChartEdit(
        "Visit",
        visit_id,
        EditAction.AMEND,
        {"appointment_id": appointment_id},
        reason or "linked encounter to appointment",
    )
    return apply_edits(session, actor, [edit])[0].detail


def build_scheduling_tools(session, *, identity=None) -> list:
    """The agent's appointment toolset. Needs a session to read and write; absent
    one (or the Anthropic SDK) it offers nothing, so the baseline is unchanged."""
    try:
        from anthropic import beta_tool
    except ImportError:
        return []
    if session is None:
        return []

    from hdh.core.models import tool_guard

    guard = tool_guard(session)

    @beta_tool
    @guard
    def find_appointments(mrn: str = "", provider: str = "", since: str = "", until: str = "") -> str:
        """Find appointments on the calendar — for a patient (by MRN), a provider (name or identifier), or both, optionally within a date window. Use this to show an upcoming schedule or to get the appointment id before rescheduling or cancelling. Give at least a patient or a provider.

        Args:
            mrn: The patient's medical record number, to list their appointments. Optional.
            provider: A provider's name or identifier, to list their calendar. Optional.
            since: Only appointments at or after this date/time (ISO 8601, e.g. 2026-10-27). Optional.
            until: Only appointments before this date/time (ISO 8601). Optional.
        """
        return _find(session, mrn, provider, since, until)

    @beta_tool
    @guard
    def book_appointment(
        mrn: str, provider: str, start: str, duration_minutes: int = 0, reason: str = ""
    ) -> str:
        """Book an appointment for a patient with a provider. Refuses if it would double-book the provider (an overlapping scheduled/confirmed slot), naming the clash; flags a slot outside the clinic's hours but still books it. Report the new appointment's id back.

        Args:
            mrn: The patient's medical record number.
            provider: The provider's name or identifier.
            start: When it begins, ISO 8601 (e.g. 2026-10-27 09:00). Resolve any relative day to a real date first.
            duration_minutes: How long, in minutes (default 30).
            reason: Why the patient is being seen, in a few words.
        """
        return _book(session, identity, _Booking(mrn, provider, start, duration_minutes, reason))

    @beta_tool
    @guard
    def reschedule_appointment(
        appointment_id: int, start: str, reason: str, duration_minutes: int = 0
    ) -> str:
        """Move an appointment to a new time. Refuses if the new slot would double-book the provider. Keeps the same patient and provider. A reason for the change is required and recorded on the audit trail.

        Args:
            appointment_id: The appointment's id (from find_appointments).
            start: The new start, ISO 8601 (e.g. 2026-10-27 14:30).
            reason: Why it is being moved (recorded on the audit trail).
            duration_minutes: New length in minutes; omit to keep the current length.
        """
        return _reschedule(session, identity, appointment_id, start, reason, duration_minutes)

    @beta_tool
    @guard
    def cancel_appointment(appointment_id: int, reason: str) -> str:
        """Cancel a scheduled or confirmed appointment — it stays on the record as cancelled (not deleted) with the reason. A reason is required and recorded on the audit trail.

        Args:
            appointment_id: The appointment's id (from find_appointments).
            reason: Why it is being cancelled (recorded on the audit trail).
        """
        return _cancel(session, identity, appointment_id, reason)

    @beta_tool
    @guard
    def link_encounter_to_appointment(visit_id: int, appointment_id: int, reason: str = "") -> str:
        """Record that an encounter (visit) fulfilled a booked appointment — sets the visit's appointment link. Use after charting a visit that came from a booked slot.

        Args:
            visit_id: The encounter/visit id.
            appointment_id: The appointment it fulfilled.
            reason: Why the link is being recorded. Optional.
        """
        return _link(session, identity, visit_id, appointment_id, reason)

    return [
        find_appointments,
        book_appointment,
        reschedule_appointment,
        cancel_appointment,
        link_encounter_to_appointment,
    ]
