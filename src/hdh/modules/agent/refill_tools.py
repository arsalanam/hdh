"""Milestone C of `medication-orders-and-refills.md`: refills, conversationally.

A patient asks for a repeat. The agent looks up the authorisation, asks
whether it still permits a fill, and either records the supply or explains
why not — in the patient's terms, from the chart, with the reason coming
from arithmetic rather than from the model.

**The agent does not decide.** `hdh.core.refills.can_refill` does, and it is
deterministic: open order, not expired, refills remaining. The agent asks
and records the outcome. That split is the point of the milestone — a
refusal a patient is given has to be one somebody can re-derive tomorrow and
argue with, and a model's opinion is neither.

**A deliberate, bounded relaxation.** #121 established that the agent places
requests and does not write the chart. A fill is the exception, decided by
§7 Q3 — *outcomes only, the agent acts and the fill records what happened* —
because a refill queue is workflow and the brief was realism without it.

The relaxation is narrow and guarded three ways:

1. a fill is written **only** through :func:`~hdh.core.refills.record_fill`,
   which refuses unless `can_refill` allows and writes nothing when it
   refuses;
2. every fill carries ``origin=AGENT`` for as long as the row exists, so an
   agent-initiated supply never becomes indistinguishable from a
   clinician's;
3. nothing else in this pack writes anything — no conditions, no
   prescriptions, no labs, no new authorisations. The agent cannot author
   the permission it then acts on.
"""

from __future__ import annotations

from datetime import date

from hdh.core.refills import decide_refill, find_authorising_order, record_fill, remaining_refills


def _as_of(session) -> date:
    """The chart's own reference date, not the wall clock.

    A synthetic chart generated last year is not stale, it is dated — the
    same anchor `caregaps` and `careplan` use, so all three agree about what
    "now" means for a patient.
    """
    from hdh.modules.caregaps import reference_date

    return reference_date(session)


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    return session.query(Patient).filter(Patient.mrn == mrn).first()


def _describe(session, order, as_of: date) -> str:
    """One order, with what is left on it and why."""
    from hdh.core.refills import issued_count

    decision = decide_refill(session, order, as_of)
    issued = issued_count(session, order)
    authorised = order.refills_authorised
    left = "—" if authorised is None else remaining_refills(authorised, issued)
    expiry = f", valid until {order.valid_until}" if order.valid_until else ""
    return (
        f"  #{order.id} {order.display}\n"
        f"     ordered {order.requested_date}{expiry}; "
        f"{issued} fill(s) issued, {left} refill(s) left\n"
        f"     {'refillable' if decision else 'not refillable'} — {decision.reason}"
    )


def build_refill_tools(session) -> list:
    """The agent's medication-refill toolset."""
    try:
        from anthropic import beta_tool
    except ImportError:
        return []

    from hdh.core.models import tool_guard

    guard = tool_guard(session)

    @beta_tool
    @guard
    def list_medication_orders(mrn: str) -> str:
        """List a patient's medication authorisations and what is left on each: how many fills have been issued, how many refills remain, whether it is still refillable and why not. Use this when the user asks what a patient is authorised for, or before discussing a refill.

        Args:
            mrn: The patient's medical record number.
        """
        from hdh.core.models import ServiceKind, ServiceRequest

        patient = _patient(session, mrn)
        if patient is None:
            return f"no patient {mrn}"

        as_of = _as_of(session)
        orders = (
            session.query(ServiceRequest)
            .filter(
                ServiceRequest.patient_id == patient.id,
                ServiceRequest.kind == ServiceKind.MEDICATION,
                ServiceRequest.voided_at.is_(None),
            )
            .order_by(ServiceRequest.requested_date.desc())
            .all()
        )
        if not orders:
            return f"{mrn} has no medication orders on record"
        lines = [f"medication orders for {mrn} (as of {as_of}):"]
        lines.extend(_describe(session, order, as_of) for order in orders)
        return "\n".join(lines)

    @beta_tool
    @guard
    def check_medication_refill(mrn: str, drug_name: str) -> str:
        """Ask whether a medication may be refilled, and get the reason either way. Changes nothing. Always call this before recording a refill, and give the user the reason verbatim — it comes from the chart, not from you.

        Args:
            mrn: The patient's medical record number.
            drug_name: The medication the patient is asking about.
        """
        patient = _patient(session, mrn)
        if patient is None:
            return f"no patient {mrn}"

        as_of = _as_of(session)
        order = find_authorising_order(session, patient.id, drug_name, as_of)
        decision = decide_refill(session, order, as_of)
        if order is None:
            return f"{drug_name}: {decision.reason}"
        verdict = "may be refilled" if decision else "may NOT be refilled"
        return f"{order.display} (order #{order.id}) {verdict} — {decision.reason}"

    @beta_tool
    @guard
    def refill_medication(mrn: str, drug_name: str, days_supply: int = 0) -> str:
        """Record that a refill was supplied. Only succeeds if the authorisation still permits one; otherwise nothing is written and you get the reason to give the patient. The fill is recorded as agent-initiated, permanently. Never tell a patient a refill was issued unless this call reports that it was.

        Args:
            mrn: The patient's medical record number.
            drug_name: The medication being refilled.
            days_supply: Days of medication supplied, if known. 0 to omit.
        """
        from hdh.core.models import RequestOrigin

        patient = _patient(session, mrn)
        if patient is None:
            return f"no patient {mrn}"

        as_of = _as_of(session)
        order = find_authorising_order(session, patient.id, drug_name, as_of)
        decision, dispense = record_fill(
            session,
            order,
            as_of,
            origin=RequestOrigin.AGENT,
            days_supply=days_supply or None,
        )
        if dispense is None:
            return f"refill refused for {drug_name}: {decision.reason}. Nothing was recorded."
        session.commit()
        return (
            f"refill recorded: {dispense.drug_name}, dispensed {dispense.dispensed_date}, "
            f"against order #{order.id} (dispense #{dispense.id}, origin agent). "
            f"{decision.reason} after this fill."
        )

    return [list_medication_orders, check_medication_refill, refill_medication]
