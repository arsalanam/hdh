"""A chart edit carries the signed-in provider (AU4 follow-on, #169).

Before login, an agent-made amend was attributed by name-matching a provider
mentioned in the reason text, falling back to "agent". Now that the agent runs
as a signed-in identity, an amend or void carries *that* person — the username
in the trail and a real provider_id on the event — and a role that may not
edit the chart is refused. The no-identity path (eval/headless) keeps the old
name-match fallback.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.identity import Identity
from hdh.core.models import (
    Condition,
    ConditionStatus,
    Patient,
    Provider,
    Sex,
    get_engine,
    get_session,
)
from hdh.core.schema_registry import bootstrap_schema


@pytest.fixture()
def chart(tmp_path):
    pytest.importorskip("anthropic")
    from hdh.core.models import Base

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "amend.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    patient = Patient(
        mrn="MRN1", first_name="A", last_name="B", date_of_birth=date(1980, 1, 1), sex=Sex.FEMALE
    )
    session.add(patient)
    session.flush()
    condition = Condition(
        patient_id=patient.id,
        icd10_code="E11.9",
        description="Type 2 diabetes",
        chronic=True,
        status=ConditionStatus.ACTIVE,
        onset_date=date(2024, 1, 1),
    )
    session.add(condition)
    session.commit()
    yield session, patient, condition
    session.close()
    engine.dispose()


def _linked(session, username, roles, provider_name):
    """A provider profile + the linked Identity that resolves to it."""
    from hdh.core.identity.accounts import link

    provider = Provider(identifier=f"HDH-{username}", name=provider_name)
    session.add(provider)
    session.flush()
    link(session, f"sub-{username}", username, provider.id)
    return Identity(f"sub-{username}", username, frozenset(roles), provider_id=provider.id), provider


def _amend(session, identity, entity, row_id, changes, reason):
    from hdh.modules.agent.chart_tools import build_chart_tools

    tool = next(t for t in build_chart_tools(session, identity=identity) if t.name == "amend_chart_entry")
    import json

    return tool.call({"entity": entity, "row_id": row_id, "changes": json.dumps(changes), "reason": reason})


def _last_event(session, entity, row_id):
    from hdh.core.models import ChartAuditEvent

    return (
        session.query(ChartAuditEvent)
        .filter_by(entity=entity, row_id=row_id)
        .order_by(ChartAuditEvent.id.desc())
        .first()
    )


def test_an_amend_is_attributed_to_the_signed_in_provider(chart):
    session, _patient, condition = chart
    chen, provider = _linked(session, "dr.chen", ["clinician"], "Dr. Grace Chen")
    out = _amend(session, chen, "Condition", condition.id, {"controlled": True}, "reviewed with patient")
    assert '"applied": true' in out.lower() or '"applied": true' in out

    event = _last_event(session, "Condition", condition.id)
    assert event.actor_name == "dr.chen"  # the login, not a name guessed from the text
    assert event.provider_id == provider.id  # a real provider_id on the event


def test_the_login_beats_a_different_name_in_the_reason(chart):
    """The old path guessed from the reason text; the login must win over it."""
    session, _patient, condition = chart
    # seed another provider whose surname appears in the reason
    session.add(Provider(identifier="HDH-OTHER", name="Dr. Priya Sharma, MD"))
    session.flush()
    chen, provider = _linked(session, "dr.chen", ["clinician"], "Dr. Grace Chen")
    _amend(session, chen, "Condition", condition.id, {"controlled": True}, "per Dr. Sharma's note")
    event = _last_event(session, "Condition", condition.id)
    assert event.actor_name == "dr.chen" and event.provider_id == provider.id


def test_a_nurse_may_not_void_a_chart_row(chart):
    """Attribution and authorization are the same login: a nurse holds
    chart:edit but not chart:void, and the void is refused with the reason."""

    from hdh.modules.agent.chart_tools import build_chart_tools

    session, _patient, condition = chart
    nurse, _ = _linked(session, "nurse.reed", ["nurse"], "Sam Reed")
    tool = next(t for t in build_chart_tools(session, identity=nurse) if t.name == "void_chart_entry")
    out = tool.call({"entity": "Condition", "row_id": condition.id, "reason": "entered in error"})
    assert "chart:void" in out and "not" in out.lower()
    # and nothing was voided
    assert session.get(Condition, condition.id).voided_at is None


def test_a_nurse_may_amend(chart):
    """The same nurse DOES hold chart:edit, so an amend goes through and is
    attributed to them."""
    session, _patient, condition = chart
    nurse, provider = _linked(session, "nurse.reed", ["nurse"], "Sam Reed")
    _amend(session, nurse, "Condition", condition.id, {"controlled": True}, "vitals reviewed")
    event = _last_event(session, "Condition", condition.id)
    assert event.actor_name == "nurse.reed" and event.provider_id == provider.id


def test_no_identity_keeps_the_name_match_fallback(chart):
    """The eval/headless path: no login, so the reason-text name-match still
    attributes — the behaviour that predates authentication."""
    session, _patient, condition = chart
    session.add(Provider(identifier="HDH-SHARMA", name="Dr. Priya Sharma, MD"))
    session.flush()
    _amend(session, None, "Condition", condition.id, {"controlled": True}, "confirmed by Dr. Sharma")
    event = _last_event(session, "Condition", condition.id)
    assert event.actor_name == "Dr. Priya Sharma, MD"
