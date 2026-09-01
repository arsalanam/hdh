"""Milestone C: refills through the agent, and what it still may not do.

The demonstration is a chart that moves because an agent acted. The point of
these tests is the boundary around that: the agent asks, the arithmetic
decides, and a refusal writes nothing at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.modules.agent.refill_tools import build_refill_tools

AS_OF = date(2026, 6, 1)


def _tool(tools, name: str):
    return next(tool for tool in tools if tool.name == name)


def _names(tools) -> set[str]:
    names = {tool.name for tool in tools}
    assert names and all(names), "no tool names — an absence test here would be vacuous"
    return names


@pytest.fixture()
def chart(tmp_path, monkeypatch):
    """One patient, one authorisation with two refills, already filled once."""
    from hdh.core.models import (
        Base,
        MedicationDispense,
        Patient,
        RequestOrigin,
        RequestStatus,
        ServiceKind,
        ServiceRequest,
        Sex,
        get_engine,
        get_session,
    )
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)

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
    session.add(
        MedicationDispense(
            patient_id=patient.id,
            request_id=order.id,
            drug_name="Atorvastatin 20mg",
            dispensed_date=date(2026, 1, 1),
            origin="GENERATED",
        )
    )
    session.commit()

    # The chart's own "now", so the tests do not drift with the wall clock.
    monkeypatch.setattr("hdh.modules.agent.refill_tools._as_of", lambda _s: AS_OF)
    yield session, build_refill_tools(session), order
    session.close()
    engine.dispose()


# ── the boundary ─────────────────────────────────────────────────────────


def test_the_pack_cannot_create_an_authorisation(chart):
    """The agent must not be able to author the permission it then acts on."""
    _session, tools, _order = chart
    offenders = {
        name
        for name in _names(tools)
        if ("order" in name or "authoris" in name) and ("create" in name or "add" in name)
    }
    assert not offenders, f"the agent gained a tool that writes its own permission: {offenders}"


def test_the_pack_writes_nothing_but_fills(chart):
    """No conditions, no prescriptions, no labs. #121's guarantee holds
    everywhere except the one place §7 Q3 opened."""
    _session, tools, _order = chart
    forbidden = ("condition", "prescription", "lab", "visit", "allerg")
    offenders = {
        name
        for name in _names(tools)
        if any(word in name for word in forbidden) and ("add" in name or "create" in name)
    }
    assert not offenders, f"the refill pack reached outside medications: {offenders}"


def test_a_refusal_writes_nothing(chart):
    """The refusal is the whole outcome. A partially-applied refusal would
    be worse than either answer."""
    from hdh.core.models import MedicationDispense

    session, tools, order = chart
    order.refills_authorised = None
    session.flush()
    before = session.query(MedicationDispense).count()

    result = _tool(tools, "refill_medication")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "refused" in result and "Nothing was recorded" in result
    assert session.query(MedicationDispense).count() == before


def test_a_fill_is_permanently_marked_as_agent_initiated(chart):
    """The property that makes letting an agent write here acceptable."""
    from hdh.core.models import MedicationDispense

    session, tools, _order = chart
    _tool(tools, "refill_medication")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    latest = session.query(MedicationDispense).order_by(MedicationDispense.id.desc()).first()
    assert latest.origin == "agent"


# ── the agent asks; the arithmetic decides ───────────────────────────────


def test_checking_changes_nothing(chart):
    from hdh.core.models import MedicationDispense

    session, tools, _order = chart
    before = session.query(MedicationDispense).count()
    _tool(tools, "check_medication_refill")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert session.query(MedicationDispense).count() == before


def test_the_check_reports_what_is_left(chart):
    _session, tools, _order = chart
    text = _tool(tools, "check_medication_refill")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "may be refilled" in text
    assert "2 refill(s) remaining" in text


def test_refills_run_out_and_then_refuse(chart):
    """One authorisation, two refills, three fills in total."""
    _session, tools, _order = chart
    refill = _tool(tools, "refill_medication")
    assert "refill recorded" in refill(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "refill recorded" in refill(mrn="MRN-REFILL", drug_name="Atorvastatin")
    exhausted = refill(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "refused" in exhausted
    assert "3 of 3" in exhausted


def test_an_expired_authorisation_refuses_with_its_own_reason(chart):
    session, tools, order = chart
    order.valid_until = date(2026, 3, 1)
    session.flush()
    text = _tool(tools, "refill_medication")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "expired on 2026-03-01" in text


def test_a_drug_that_was_never_prescribed_says_so(chart):
    _session, tools, _order = chart
    text = _tool(tools, "check_medication_refill")(mrn="MRN-REFILL", drug_name="Warfarin")
    assert "no authorising order" in text


def test_an_unknown_patient_is_said_plainly(chart):
    _session, tools, _order = chart
    assert "no patient" in _tool(tools, "check_medication_refill")(mrn="NOPE", drug_name="X")


# ── the order lookup ─────────────────────────────────────────────────────


def test_a_closed_order_is_still_found_so_the_refusal_can_be_specific(chart):
    """ "Closed on 2026-03-01" is more use to a patient than "no order on
    record", which would be untrue."""
    session, tools, order = chart
    order.end_date = date(2026, 3, 1)
    session.flush()
    text = _tool(tools, "check_medication_refill")(mrn="MRN-REFILL", drug_name="Atorvastatin")
    assert "2026-03-01" in text
    assert "no authorising order" not in text


def test_listing_shows_the_authorisation_and_what_is_left(chart):
    _session, tools, _order = chart
    text = _tool(tools, "list_medication_orders")(mrn="MRN-REFILL")
    assert "Atorvastatin 20mg" in text
    assert "1 fill(s) issued, 2 refill(s) left" in text
    assert "refillable" in text


def test_the_pack_is_registered_with_the_agent():
    from hdh.modules.agent.tools import _ONTOLOGY_BUILDERS

    assert ("hdh.modules.agent.refill_tools", "build_refill_tools") in _ONTOLOGY_BUILDERS
