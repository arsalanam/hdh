"""The agent acts as a signed-in provider (AU4).

AU1–AU3 built identity, linking and permissions; this is where the agent's
own writes finally carry a real person and are refused when the person's
roles do not permit them. The headline scenario the attribution design
opened with — a plan authored by one clinician and amended by another —
now shows both names in the trail, because each write is attributed to
whoever is signed in when it happens (design §2.4: authorization
re-resolved at action time).

Enforcement is exercised through the careplan record tools and the refill
tool with a real (fake-provider) identity; the None path — the eval harness
and headless tests — keeps working under a system actor.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.identity import Identity


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=4, years_of_history=2, verbose=False, seed=11, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _values(n=2):
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft

    names = ["Polypharmacy", "Falls risk"][:n]
    return {
        "concerns": [ConcernDraft(f"{x} needs review", "risk", (f"s/{i}",)) for i, x in enumerate(names)],
        "goals": [GoalDraft(f"Improve {x}", i, "", (f"s/{i}",)) for i, x in enumerate(names)],
        "interventions": [
            InterventionDraft(f"Act on {x}", i, "service", "GP", (f"s/{i}",)) for i, x in enumerate(names)
        ],
        "deferred": [],
    }


def _linked(session, username, roles, provider_name):
    """A provider profile + an account link, and the Identity that resolves
    to it — what a real login yields after AU2 seeding."""
    from hdh.core.identity.accounts import link
    from hdh.core.models import Provider

    provider = Provider(identifier=f"HDH-DEMO-{username}", name=provider_name)
    session.add(provider)
    session.flush()
    subject = f"sub-{username}"
    link(session, subject, username, provider.id)
    return Identity(subject, username, frozenset(roles), provider_id=provider.id), provider


# ── enforcement: the record tools refuse what a role cannot do ───────────


def test_a_nurse_cannot_approve_a_plan(chart):
    from hdh.modules.careplan.agent_tools import _decide
    from hdh.modules.careplan.persist import persist_reviewed_plan

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    nurse, _ = _linked(session, "nurse.reed", ["nurse"], "Sam Reed")
    out = _decide(session, patient.mrn, plan_id, True, "looks fine", identity=nurse)
    assert "not authorized" in out
    assert "careplan:approve" in out
    # and the plan is untouched
    from hdh.modules.careplan.persist import load_plan

    assert load_plan(session, plan_id)["row"].status == "user_edited"


def test_a_clinician_can_approve_a_plan(chart):
    from hdh.modules.careplan.agent_tools import _decide
    from hdh.modules.careplan.persist import load_plan, persist_reviewed_plan

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    chen, _ = _linked(session, "dr.chen", ["clinician"], "Dr. Grace Chen")
    out = _decide(session, patient.mrn, plan_id, True, "reviewed with patient", identity=chen)
    assert "approved" in out
    assert load_plan(session, plan_id)["row"].status == "approved"


def test_a_nurse_cannot_amend_a_saved_plan(chart):
    from hdh.modules.careplan.agent_tools import _amend_saved
    from hdh.modules.careplan.persist import persist_reviewed_plan

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    nurse, _ = _linked(session, "nurse.reed", ["nurse"], "Sam Reed")
    out = _amend_saved(session, patient.mrn, "1", plan_id, "", identity=nurse)
    assert "not authorized" in out and "careplan:amend" in out


# ── attribution: the trail names the person and carries provider_id ──────


def test_an_approval_is_attributed_to_the_clinician(chart):
    from hdh.modules.careplan.agent_tools import _decide
    from hdh.modules.careplan.persist import history, persist_reviewed_plan

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    chen, provider = _linked(session, "dr.chen", ["clinician"], "Dr. Grace Chen")
    _decide(session, patient.mrn, plan_id, True, "signed off", identity=chen)

    approve = next(e for e in history(session, plan_id) if e["action"] == "approve")
    assert approve["actor"] == "dr.chen"

    from hdh.core.models import ChartAuditEvent

    row = (
        session.query(ChartAuditEvent)
        .filter_by(row_id=plan_id, entity="CarePlan")
        .order_by(ChartAuditEvent.id.desc())
        .first()
    )
    assert row.provider_id == provider.id, "the event carries the provider, not just a name"


def test_authored_by_one_amended_by_another(chart):
    """The scenario the attribution design opened with. Each write is
    attributed to whoever is signed in when it happens."""
    from hdh.modules.careplan.agent_tools import _amend_saved, _decide
    from hdh.modules.careplan.persist import history, persist_reviewed_plan

    session, patient = chart
    chen, _ = _linked(session, "dr.chen", ["clinician"], "Dr. Grace Chen")
    okafor, _ = _linked(session, "dr.okafor", ["clinician"], "Dr. Ada Okafor")

    # A authors and approves
    plan_id = persist_reviewed_plan(session, patient, _values(), actor=_actor_for(session, chen)).plan_id
    _decide(session, patient.mrn, plan_id, True, "signed off", identity=chen)

    # B amends the approved plan — supersedes it
    out = _amend_saved(session, patient.mrn, "1", plan_id, "dropping falls", identity=okafor)
    assert "superseding it" in out

    old_trail = history(session, plan_id)
    assert old_trail[0]["actor"] == "dr.chen"  # authored by A
    assert old_trail[-1]["action"] == "amend"  # superseded, by B
    # the successor's create is B's
    from hdh.core.models import ChartAuditEvent

    successor = (
        session.query(ChartAuditEvent)
        .filter(
            ChartAuditEvent.entity == "CarePlan",
            ChartAuditEvent.action
            == __import__("hdh.core.models", fromlist=["AuditAction"]).AuditAction.CREATE,
        )
        .order_by(ChartAuditEvent.id.desc())
        .first()
    )
    assert successor.actor_name == "dr.okafor"


def _actor_for(session, identity):
    from hdh.core.identity import resolve_actor
    from hdh.core.models import EditSource

    return resolve_actor(session, identity, EditSource.AGENT)


# ── the system path (None identity) still works ──────────────────────────


def test_no_identity_is_a_system_context(chart):
    """The eval harness and headless tests pass no identity; writes proceed
    under a system actor rather than being refused."""
    from hdh.modules.careplan.agent_tools import _decide
    from hdh.modules.careplan.persist import history, persist_reviewed_plan

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    out = _decide(session, patient.mrn, plan_id, True, "eval", identity=None)
    assert "approved" in out
    approve = next(e for e in history(session, plan_id) if e["action"] == "approve")
    assert approve["actor"] == "care-plan review"  # the honest system label


# ── the refill tool enforces medication:fill ─────────────────────────────


def test_a_nurse_cannot_refill(chart):
    from hdh.core.models import (
        RequestOrigin,
        RequestStatus,
        ServiceKind,
        ServiceRequest,
        Visit,
        VisitType,
    )
    from hdh.modules.agent.refill_tools import build_refill_tools

    session, patient = chart
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 1, 1), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add(
        ServiceRequest(
            patient_id=patient.id,
            visit_id=visit.id,
            kind=ServiceKind.MEDICATION,
            display="Atorvastatin 20mg",
            status=RequestStatus.ACTIVE,
            origin=RequestOrigin.CLINICIAN,
            requested_date=date(2026, 1, 1),
            refills_authorised=3,
        )
    )
    session.commit()
    nurse, _ = _linked(session, "nurse.reed", ["nurse"], "Sam Reed")

    tools = {t.name: t for t in build_refill_tools(session, identity=nurse)}
    out = tools["refill_medication"](mrn=patient.mrn, drug_name="Atorvastatin")
    assert "not authorized" in out and "medication:fill" in out


def test_a_prescriber_can_refill_and_is_attributed(chart):
    from hdh.core.models import (
        ChartAuditEvent,
        RequestOrigin,
        RequestStatus,
        ServiceKind,
        ServiceRequest,
        Visit,
        VisitType,
    )
    from hdh.modules.agent.refill_tools import build_refill_tools

    session, patient = chart
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 1, 1), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add(
        ServiceRequest(
            patient_id=patient.id,
            visit_id=visit.id,
            kind=ServiceKind.MEDICATION,
            display="Atorvastatin 20mg",
            status=RequestStatus.ACTIVE,
            origin=RequestOrigin.CLINICIAN,
            requested_date=date(2026, 1, 1),
            refills_authorised=3,
        )
    )
    session.commit()
    okafor, provider = _linked(session, "dr.okafor", ["prescriber"], "Dr. Ada Okafor")

    tools = {t.name: t for t in build_refill_tools(session, identity=okafor)}
    out = tools["refill_medication"](mrn=patient.mrn, drug_name="Atorvastatin")
    assert "refill recorded" in out
    event = (
        session.query(ChartAuditEvent)
        .filter_by(entity="MedicationDispense")
        .order_by(ChartAuditEvent.id.desc())
        .first()
    )
    assert event.actor_name == "dr.okafor"
    assert event.provider_id == provider.id


# ── the login gate ───────────────────────────────────────────────────────


def test_the_agent_refuses_without_login(monkeypatch):
    """§4.4: no anonymous agent sessions. `_require_login` exits when nobody
    is signed in — the gate every agent entry passes through."""
    from hdh.modules.agent import cli

    monkeypatch.setattr("hdh.core.identity.current_identity", lambda *_a, **_k: None)
    with pytest.raises(SystemExit) as e:
        cli._require_login()
    assert "hdh login" in str(e.value)
