"""The appointments store — pure queries and conflict checks over the core
``Appointment`` model (design: appointments feature).

These functions mutate the session (add / flush) but never commit — the caller
(an agent tool, a backend, a reminder sweep) owns the transaction, so the same
helpers compose into any surface and are unit-tested against an in-memory chart.
Rescheduling and cancelling are NOT here: they go through the audited chart-edit
path (:func:`hdh.core.chartedit.apply_edits`) so the calendar's history is as
accountable as the chart's; this module provides the reads and the conflict/clinic
checks those tools consult first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewAppointment:
    """The fields needed to book a slot — grouped so the create helper and the
    tool that calls it stay single-purpose (mirrors ``order_tools._OrderDraft``)."""

    patient_id: int
    provider_id: int
    start_at: datetime
    end_at: datetime
    reason: str | None = None
    location_id: int | None = None
    created_by_id: int | None = None


def overlapping(session, provider_id: int, start: datetime, end: datetime, *, exclude_id: int | None = None):
    """Active (scheduled|confirmed) appointments for this provider whose slot
    overlaps ``[start, end)`` — the double-booking check. Half-open overlap
    (``existing.start < end AND existing.end > start``) so back-to-back slots
    that merely touch do not clash. ``exclude_id`` skips the row being moved."""
    from hdh.core.models import Appointment

    query = session.query(Appointment).filter(
        Appointment.provider_id == provider_id,
        Appointment.status.in_(Appointment.ACTIVE_STATUSES),
        Appointment.start_at < end,
        Appointment.end_at > start,
    )
    if exclude_id is not None:
        query = query.filter(Appointment.id != exclude_id)
    return query.order_by(Appointment.start_at).all()


def clinic_hours_warning(session, provider_id: int, start: datetime, end: datetime) -> str | None:
    """A soft note when the slot falls outside the provider's clinic hours, or
    ``None`` when it fits or the hours are unknown. Best-effort and never
    blocking: booking out-of-hours is allowed (home visits, catch-up clinics),
    the clinician just sees the flag. Walks provider → primary location →
    location-specialty → :class:`ServiceHours` for the weekday."""
    from hdh.core.models import LocationSpecialty, Provider, ServiceHours, primary_location_id

    provider = session.get(Provider, provider_id)
    if provider is None or provider.specialty_id is None:
        return None
    location_id = primary_location_id(session, provider_id)
    if location_id is None:
        return None
    clinic = (
        session.query(LocationSpecialty)
        .filter(
            LocationSpecialty.location_id == location_id,
            LocationSpecialty.specialty_id == provider.specialty_id,
        )
        .first()
    )
    if clinic is None:
        return None
    blocks = [
        h
        for h in session.query(ServiceHours)
        .filter(ServiceHours.location_specialty_id == clinic.id, ServiceHours.day_of_week == start.weekday())
        .all()
        if h.opens and h.closes
    ]
    weekday = start.strftime("%A")
    if not blocks:
        return f"note: {weekday} is outside this clinic's recorded hours"
    if any(h.opens <= start.time() and end.time() <= h.closes for h in blocks):
        return None
    windows = ", ".join(f"{h.opens:%H:%M}–{h.closes:%H:%M}" for h in blocks)
    return f"note: {start:%H:%M}–{end:%H:%M} is outside this clinic's {weekday} hours ({windows})"


def find(
    session,
    *,
    patient_id: int | None = None,
    provider_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    status: str | None = None,
):
    """Appointments matching the given filters, soonest first. With no date
    bounds it returns the whole history for the patient/provider; callers that
    want "upcoming" pass ``since=now``."""
    from hdh.core.models import Appointment

    query = session.query(Appointment)
    if patient_id is not None:
        query = query.filter(Appointment.patient_id == patient_id)
    if provider_id is not None:
        query = query.filter(Appointment.provider_id == provider_id)
    if since is not None:
        query = query.filter(Appointment.start_at >= since)
    if until is not None:
        query = query.filter(Appointment.start_at < until)
    if status:
        query = query.filter(Appointment.status == status)
    return query.order_by(Appointment.start_at).all()


def create(session, new: NewAppointment):
    """Insert one ``scheduled`` appointment and flush so the row has an id (for
    the audit event). Does not commit — the caller owns the transaction."""
    from hdh.core.models import Appointment

    appointment = Appointment(
        patient_id=new.patient_id,
        provider_id=new.provider_id,
        location_id=new.location_id,
        start_at=new.start_at,
        end_at=new.end_at,
        status="scheduled",
        reason=new.reason,
        created_by_id=new.created_by_id,
    )
    session.add(appointment)
    session.flush()
    return appointment
