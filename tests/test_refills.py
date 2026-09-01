"""May this order be filled again? (`medication-orders-and-refills.md` §4.4)

The arithmetic is small; what these tests are really pinning is that every
refusal names its own cause. This answer gets told to a patient, so "no"
without a reason is not an acceptable output.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.refills import Decision, can_refill, decide_refill, remaining_refills

TODAY = date(2026, 8, 31)


class _Order:
    """Enough of a ServiceRequest for the rule to read."""

    def __init__(self, **kwargs):
        self.id = 1
        self.voided_at = None
        self.end_date = None
        self.status = "ACTIVE"
        self.requested_date = date(2026, 1, 1)
        self.refills_authorised = 2
        self.valid_until = None
        self.__dict__.update(kwargs)


# ── the arithmetic ───────────────────────────────────────────────────────


def test_the_first_supply_is_not_a_refill():
    """Two authorised refills means three fills in total."""
    assert remaining_refills(2, issued=1) == 2
    assert remaining_refills(2, issued=2) == 1
    assert remaining_refills(2, issued=3) == 0


def test_an_unfilled_order_has_all_its_refills():
    assert remaining_refills(3, issued=0) == 3


def test_an_over_filled_order_reports_zero_not_a_negative():
    """A negative would read as a credit."""
    assert remaining_refills(1, issued=9) == 0


def test_remaining_is_derived_not_stored():
    """The rule takes the count as an argument. Nothing decrements a column,
    which is the failure `Prescription.refills` already demonstrates."""
    order = _Order(refills_authorised=2)
    assert can_refill(order, TODAY, issued=1).remaining == 2
    assert can_refill(order, TODAY, issued=3).remaining == 0
    assert order.refills_authorised == 2, "the authorisation must not move"


# ── allowed, and auditable ───────────────────────────────────────────────


def test_an_open_order_with_refills_left_is_allowed():
    decision = can_refill(_Order(), TODAY, issued=1)
    assert decision and decision.remaining == 2
    assert "remaining" in decision.reason


def test_an_approval_also_carries_a_reason():
    """An approval nobody can audit is worth little more than a refusal
    nobody can check."""
    assert can_refill(_Order(), TODAY, issued=1).reason


# ── every refusal names its own cause ────────────────────────────────────


def test_exhausted_refills_say_how_many_were_issued():
    decision = can_refill(_Order(refills_authorised=2), TODAY, issued=3)
    assert not decision
    assert "3 of 3" in decision.reason


def test_an_expired_authorisation_is_not_a_closed_one():
    """Nobody closed it; it ran out of time. The patient needs a new
    prescription, not an explanation of who closed it."""
    decision = can_refill(_Order(valid_until=date(2026, 6, 1)), TODAY, issued=1)
    assert not decision
    assert "expired on 2026-06-01" in decision.reason
    assert "closed" not in decision.reason


def test_an_authorisation_valid_today_still_stands():
    assert can_refill(_Order(valid_until=TODAY), TODAY, issued=1)


def test_a_closed_order_fails_before_refills_are_counted():
    """Composes with is_open rather than restating it, so the reason is the
    specific one."""
    decision = can_refill(_Order(end_date=date(2026, 3, 1)), TODAY, issued=0)
    assert not decision
    assert "2026-03-01" in decision.reason
    assert decision.remaining is None, "it never got far enough to count"


def test_a_voided_order_never_happened():
    assert not can_refill(_Order(voided_at="x"), TODAY, issued=0)


def test_a_revoked_order_says_so():
    decision = can_refill(_Order(status="REVOKED"), TODAY, issued=0)
    assert not decision and "revoked" in decision.reason


def test_an_order_authorising_no_refills_is_distinguished_from_an_exhausted_one():
    """ "Records none" and "authorised zero, then used them" are different
    facts about the prescription, and a patient asking why gets a different
    answer."""
    decision = can_refill(_Order(refills_authorised=None), TODAY, issued=1)
    assert not decision
    assert "does not authorise refills" in decision.reason


def test_no_order_at_all_is_its_own_refusal():
    decision = can_refill(None, TODAY, issued=0)
    assert not decision
    assert "no authorising order" in decision.reason


def test_every_refusal_is_long_enough_to_be_an_explanation():
    """The same bar `is_open` is held to: a refusal without a cause is
    indistinguishable from a system that did not work."""
    for order, issued in (
        (_Order(voided_at="x"), 0),
        (_Order(end_date=date(2026, 3, 1)), 0),
        (_Order(status="REVOKED"), 0),
        (_Order(valid_until=date(2026, 6, 1)), 1),
        (_Order(refills_authorised=None), 1),
        (_Order(refills_authorised=1), 5),
        (None, 0),
    ):
        decision = can_refill(order, TODAY, issued=issued)
        assert not decision
        assert len(decision.reason) > 20, decision.reason


def test_a_decision_is_falsy_when_refused():
    assert not Decision(False, "no")
    assert Decision(True, "yes")


# ── counted from the chart ───────────────────────────────────────────────


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


def _order_with_dispenses(session, count: int, *, voided: int = 0):
    from hdh.core.models import (
        MedicationDispense,
        Patient,
        RequestOrigin,
        RequestStatus,
        ServiceKind,
        ServiceRequest,
        Sex,
    )

    patient = Patient(
        mrn="MRN-REFILL",
        first_name="A",
        last_name="B",
        date_of_birth=date(1950, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    order = ServiceRequest(
        patient_id=patient.id,
        kind=ServiceKind.MEDICATION,
        status=RequestStatus.ACTIVE,
        origin=RequestOrigin.GENERATED,
        display="Atorvastatin 20mg",
        requested_date=date(2026, 1, 1),
        refills_authorised=2,
    )
    session.add(order)
    session.flush()
    for index in range(count + voided):
        session.add(
            MedicationDispense(
                patient_id=patient.id,
                request_id=order.id,
                drug_name="Atorvastatin",
                dispensed_date=date(2026, 2, 1),
                origin="GENERATED",
                voided_at=date(2026, 3, 1) if index >= count else None,
            )
        )
    session.flush()
    return order


def test_the_decision_counts_the_dispenses_on_the_chart(chart):
    order = _order_with_dispenses(chart, 1)
    assert decide_refill(chart, order, TODAY).remaining == 2


def test_filling_reduces_what_is_left(chart):
    order = _order_with_dispenses(chart, 3)
    decision = decide_refill(chart, order, TODAY)
    assert not decision and decision.remaining == 0


def test_a_voided_dispense_does_not_count_against_the_patient(chart):
    """A supply entered in error never happened. Counting it would refuse a
    refill the patient is owed."""
    order = _order_with_dispenses(chart, 1, voided=2)
    assert decide_refill(chart, order, TODAY).remaining == 2


# ── milestone D: the generator issues orders and fills ───────────────────


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("refills") / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(
        session, n_patients=20, years_of_history=4, verbose=False, seed=4242, as_of=date(2026, 8, 28)
    )
    yield session, date(2026, 8, 28)
    session.close()
    engine.dispose()


def _medication_orders(session):
    from sqlalchemy import select

    from hdh.core.models import ServiceKind, ServiceRequest

    return (
        session.execute(select(ServiceRequest).where(ServiceRequest.kind == ServiceKind.MEDICATION))
        .scalars()
        .all()
    )


def test_a_repeat_authorisation_stays_open(generated):
    """An order with refills left is not over.

    Measured before this changed: 0 of 949 generated medication orders were
    refillable, because every one was closed on the day it was written. The
    refill tool would have refused every request ever made against a
    generated chart.
    """
    from hdh.core.models import RequestStatus

    session, _as_of = generated
    orders = _medication_orders(session)
    repeats = [o for o in orders if o.refills_authorised]
    assert repeats, "the generator issued no repeat authorisations"
    for order in repeats:
        assert order.status == RequestStatus.ACTIVE
        assert order.end_date is None, "a repeat with refills left is not over"


def test_a_one_off_order_still_closes_on_the_fill(generated):
    """Nothing changed for the orders that authorise nothing."""
    from hdh.core.models import RequestStatus

    session, _as_of = generated
    one_offs = [o for o in _medication_orders(session) if not o.refills_authorised]
    assert one_offs
    for order in one_offs:
        assert order.status == RequestStatus.COMPLETED
        assert order.end_date is not None


def test_every_authorisation_is_bounded_in_time(generated):
    """A repeat that never expires is one nobody has to review."""
    session, _as_of = generated
    for order in _medication_orders(session):
        assert order.valid_until is not None


def test_no_order_is_filled_beyond_what_it_authorised(generated):
    """The invariant the whole model rests on. If this fails, `can_refill`
    is answering a question the chart has already contradicted."""
    from hdh.core.refills import issued_count

    session, _as_of = generated
    for order in _medication_orders(session):
        issued = issued_count(session, order)
        assert issued <= (order.refills_authorised or 0) + 1, (
            f"order #{order.id} has {issued} fills against {order.refills_authorised or 0} authorised refills"
        )


def test_no_fill_happens_after_the_authorisation_expired(generated):
    from sqlalchemy import select

    from hdh.core.models import MedicationDispense

    session, _as_of = generated
    for order in _medication_orders(session):
        if order.valid_until is None:
            continue
        fills = session.execute(
            select(MedicationDispense.dispensed_date).where(MedicationDispense.request_id == order.id)
        ).scalars()
        for when in fills:
            assert when <= order.valid_until, f"order #{order.id} filled after it expired"


def test_no_fill_happens_in_the_future(generated):
    from sqlalchemy import func, select

    from hdh.core.models import MedicationDispense

    session, as_of = generated
    latest = session.execute(select(func.max(MedicationDispense.dispensed_date))).scalar()
    assert latest <= as_of


def test_the_chart_can_actually_be_refilled(generated):
    """The point of the milestone. C shipped a tool that could not act on a
    generated chart, because nothing in one was refillable."""
    session, as_of = generated
    refillable = [o for o in _medication_orders(session) if decide_refill(session, o, as_of).allowed]
    assert refillable, "no generated order is refillable — milestone C is unexercisable"


def test_not_every_authorised_refill_was_collected(generated):
    """A chart where every refill is taken has no non-adherence in it, which
    is the thing a care plan most often has to notice."""
    from hdh.core.refills import issued_count

    session, _as_of = generated
    repeats = [o for o in _medication_orders(session) if o.refills_authorised]
    uncollected = [o for o in repeats if issued_count(session, o) < (o.refills_authorised or 0) + 1]
    assert uncollected, "every authorised refill was collected — no lapses in the chart"
