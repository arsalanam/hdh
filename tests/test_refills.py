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
