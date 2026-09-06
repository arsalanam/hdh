"""May this medication order be filled again?

`docs/design/medication-orders-and-refills.md` §4.3–4.4, milestone B.

Arithmetic over the chart, with no model involved. The agent does not decide
whether a refill is allowed — it asks, and it records the outcome. The check
belongs in code for the same reason `stratify` does: it can be re-derived
tomorrow and argued with, and a refusal a patient is given has to be one
somebody can check.

**Refills remaining is derived, never stored.**

    issued    = count(dispenses for this order)
    remaining = refills_authorised - (issued - 1)   # the first issue is not a refill

A counter decremented in two places drifts. `Prescription.refills` already
demonstrates the failure: it records what was authorised, never moves, and
therefore reads as current when it is not.

**Every refusal names its own cause.** "Closed on 2026-03-01" is not
"expired on 2026-03-01" and neither is "no refills remaining (3 of 3
issued)". A refusal without a cause is indistinguishable from a system that
did not work, and this one gets told to a patient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from hdh.core.fulfilment import is_open


@dataclass(frozen=True)
class Decision:
    """Whether a refill may be issued, why, and how many are left.

    ``reason`` is populated even when allowed — an approval nobody can audit
    is worth little more than a refusal nobody can check. ``remaining`` is
    ``None`` when the question did not get far enough to count, which is not
    the same as zero and must not be displayed as it.
    """

    allowed: bool
    reason: str
    remaining: int | None = None

    def __bool__(self) -> bool:
        return self.allowed


def remaining_refills(refills_authorised: int | None, issued: int) -> int:
    """How many fills are left on this authorisation.

    The first supply is not a refill, so an order authorising two refills
    permits three fills in total. Never negative: an order that has somehow
    been over-filled reports zero remaining rather than a negative number
    that would read as a credit.
    """
    authorised = int(refills_authorised or 0)
    return max(0, authorised - max(0, issued - 1))


def can_refill(order, as_of: date, *, issued: int) -> Decision:
    """The decision, given an order and how many times it has been filled.

    Pure: the caller counts the dispenses. That keeps the rule testable
    without a database and keeps the query in one place —
    :func:`decide_refill` — rather than hidden inside the arithmetic.

    Composes with :func:`~hdh.core.fulfilment.is_open` rather than restating
    it, so an order that was voided, closed, revoked or postdated fails
    before refills are counted and fails with *that* reason rather than a
    generic one.
    """
    if order is None:
        return Decision(False, "no authorising order on record for this medication")

    verdict = is_open(order, as_of)
    if not verdict.ok:
        return Decision(False, verdict.reason)

    expiry = getattr(order, "valid_until", None)
    if expiry is not None and expiry < as_of:
        # Deliberately distinct from `end_date`. Nobody closed this order;
        # it ran out of time, and the patient needs a new prescription
        # rather than an explanation of who closed it.
        return Decision(False, f"the authorisation expired on {expiry}")

    authorised = getattr(order, "refills_authorised", None)
    if authorised is None:
        return Decision(
            False,
            "the order does not authorise refills — it records none, "
            "which is not the same as authorising zero and then exhausting them",
        )

    left = remaining_refills(authorised, issued)
    if left <= 0:
        return Decision(
            False,
            f"no refills remaining ({issued} of {int(authorised) + 1} authorised fills issued)",
            remaining=0,
        )
    return Decision(True, f"{left} refill(s) remaining", remaining=left)


def issued_count(session, order) -> int:
    """How many times this order has been filled.

    Voided dispenses do not count. A supply entered in error never happened,
    and leaving it in the count would refuse a refill the patient is owed.
    """
    from sqlalchemy import func, select

    from hdh.core.models import MedicationDispense

    order_id = getattr(order, "id", None)
    if order_id is None:
        return 0
    return int(
        session.execute(
            select(func.count())
            .select_from(MedicationDispense)
            .where(
                MedicationDispense.request_id == order_id,
                MedicationDispense.voided_at.is_(None),
            )
        ).scalar()
        or 0
    )


def decide_refill(session, order, as_of: date) -> Decision:
    """:func:`can_refill` with the dispense count read from the chart."""
    if order is None:
        return Decision(False, "no authorising order on record for this medication")
    return can_refill(order, as_of, issued=issued_count(session, order))


def find_authorising_order(session, patient_id: int, drug_name: str, as_of: date):
    """The medication order a refill of ``drug_name`` would draw on.

    The most recent one that is still open. Matched on ``display``, which is
    what the order carries — there is no drug reference on a
    ``ServiceRequest`` yet, and inventing one here would put a second
    identity for a drug beside `medications`.

    Returns ``None`` rather than guessing when nothing matches. "No
    authorising order on record" is a real and common answer — the patient
    may simply never have been prescribed it.
    """
    from sqlalchemy import select

    from hdh.core.models import RequestStatus, ServiceKind, ServiceRequest

    needle = (drug_name or "").strip().lower()
    if not needle:
        return None

    candidates = session.execute(
        select(ServiceRequest)
        .where(
            ServiceRequest.patient_id == patient_id,
            ServiceRequest.kind == ServiceKind.MEDICATION,
            ServiceRequest.voided_at.is_(None),
        )
        .order_by(ServiceRequest.requested_date.desc())
    ).scalars()

    fallback = None
    for order in candidates:
        if needle not in (order.display or "").lower():
            continue
        # An open order first; otherwise keep the most recent match so the
        # refusal can name what is actually wrong with it — "closed on
        # 2026-03-01" is more use than "no order on record".
        if order.status in (RequestStatus.DRAFT, RequestStatus.ACTIVE) and order.end_date is None:
            return order
        fallback = fallback or order
    return fallback


def record_fill(session, order, when: date, *, origin, days_supply=None, quantity=None):
    """Record that a supply happened, if the order permits one.

    Returns ``(decision, dispense)``. The dispense is ``None`` when refused,
    and **nothing is written** in that case — the refusal is the whole
    outcome.

    This is the one place a fill may be created. Putting the check and the
    write together means no caller can do half of it, which is the mistake
    `fulfil` was written to prevent for requests: a status moved without its
    date, discovered only when everything downstream read it wrong.

    The dispense carries ``origin`` for as long as the row exists, so a fill
    an agent recorded stays distinguishable from one a clinician did. That
    is the property that makes letting an agent write here acceptable at
    all.
    """
    from hdh.core.models import MedicationDispense

    decision = decide_refill(session, order, when)
    if not decision.allowed:
        return decision, None

    dispense = MedicationDispense(
        patient_id=order.patient_id,
        request_id=order.id,
        drug_name=order.display,
        dispensed_date=when,
        days_supply=days_supply,
        quantity=quantity,
        origin=getattr(origin, "value", origin),
    )
    session.add(dispense)
    session.flush()
    _audit_fill(session, dispense, order, origin)
    # Re-read rather than subtract: remaining is derived, and the decision
    # above was taken before this fill existed.
    return decide_refill(session, order, when), dispense


#: How a fill's provenance maps onto the audit trail's actor vocabulary.
#:
#: `RequestOrigin` says where a row came from; `EditSource` says which
#: surface made a change. They overlap but are not the same enum, and the
#: fill is the one place both are in scope. GENERATED and EXTERNAL are
#: absent deliberately — a generated fill is bulk construction and never
#: reaches here (the generator bulk-inserts dispenses), and an external one
#: would arrive through the interchange importer's own audit, not this path.
_ORIGIN_TO_SOURCE = {
    "agent": "agent",
    "clinician": "cli",
    "comprehension": "pipeline",
}


def _audit_fill(session, dispense, order, origin) -> None:
    """Write the `create` event a fill was missing (attribution A2).

    A fill was attributed on the row (`dispense.origin`) and nowhere in the
    trail, so `hdh chart history` — and "everything an agent did to this
    patient" — could not see it. That is the action with the most direct
    physical consequence, so it is the worst thing to have been silent.

    The actor here is still provenance, not a person: `actor_name` is the
    origin and `provider_id` is None until identity lands (AU2), at which
    point a real `Identity` replaces both. Recording the trail now, and the
    name later, is the right order — the event that never happened cannot be
    backfilled.
    """
    from hdh.core.models import AuditAction, ChartAuditEvent

    origin_value = getattr(origin, "value", origin)
    source = _ORIGIN_TO_SOURCE.get(origin_value)
    if source is None:
        # Not a surface that edits a chart (generated/external). Nothing to
        # attribute, and inventing a source would be worse than the silence.
        return

    session.add(
        ChartAuditEvent(
            actor_name=f"refill ({origin_value})",
            actor_source=source,
            patient_id=dispense.patient_id,
            entity="MedicationDispense",
            row_id=dispense.id,
            action=AuditAction.CREATE,
            reason=f"refill dispensed against order #{order.id}",
            before=None,
            after={
                "drug_name": dispense.drug_name,
                "dispensed_date": dispense.dispensed_date.isoformat() if dispense.dispensed_date else None,
                "days_supply": dispense.days_supply,
                "origin": origin_value,
            },
        )
    )
    session.flush()
