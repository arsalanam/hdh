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
