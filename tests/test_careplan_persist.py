"""A reviewed plan becomes a record, with a decision and a trail.

Measured before this existed: a full agent review session left 14
checkpoints and **zero** rows in `care_plan_records`, `health_concerns`,
`plan_goals` and `plan_interventions`. The plan a clinician had just built
and edited existed only in a LangGraph checkpoint.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.modules.careplan.persist import (
    APPROVED,
    ENTITY,
    REJECTED,
    USER_EDITED,
    decide,
    history,
    latest_plan_id,
    persist_reviewed_plan,
)


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=5, years_of_history=2, verbose=False, seed=11, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _values():
    """Plan state as the graph leaves it: concerns, goals, interventions."""
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft

    return {
        "concerns": [
            ConcernDraft("Polypharmacy — 6 active medications", "risk", ("med_safety/duplicate",)),
            ConcernDraft("Uncontrolled osteoarthritis", "condition", ("guidelines/oa",)),
        ],
        "goals": [
            GoalDraft("Reduce the regimen to 4 agents", 0, "4 agents", ("med_safety/duplicate",)),
            GoalDraft("Walk to the shop unaided", 1, "", ("guidelines/oa",)),
        ],
        "interventions": [
            InterventionDraft("Structured medication review", 0, "service", "GP", ("med_safety/duplicate",)),
            InterventionDraft("Refer to physiotherapy", 1, "referral", "GP", ("guidelines/oa",)),
        ],
        "deferred": ["Essential hypertension — controlled"],
    }


# ── the plan becomes a row ───────────────────────────────────────────────


def test_a_reviewed_plan_is_written(chart):
    session, patient = chart
    decision = persist_reviewed_plan(session, patient, _values(), thread_id="t1")
    assert decision and decision.plan_id

    from sqlalchemy import func, select

    from hdh.core.models import Base

    tables = Base.metadata.tables
    for name, expected in (
        ("care_plan_records", 1),
        ("health_concerns", 2),
        ("plan_goals", 2),
        ("plan_interventions", 2),
    ):
        count = session.execute(select(func.count()).select_from(tables[name])).scalar()
        assert count == expected, f"{name}: {count} rows, expected {expected}"


def test_a_reviewed_plan_is_recorded_as_user_edited(chart):
    """Not `ai_generated`. A clinician approved or amended every stage, and
    recording it as machine output would misattribute their decisions."""
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    plans = Base.metadata.tables["care_plan_records"]
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    assert row.status == USER_EDITED


def test_the_plan_records_which_prompts_produced_it(chart):
    """`prompt_set` exists so a plan can say what produced it, and was never
    populated on the review path because the review path never wrote a row."""
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    plans = Base.metadata.tables["care_plan_records"]
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    assert row.prompt_set, "the plan cannot say which wording produced it"


def test_the_thread_is_kept_so_the_run_can_be_resumed(chart):
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values(), thread_id="thread-42").plan_id
    plans = Base.metadata.tables["care_plan_records"]
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    assert row.checkpoint_thread_id == "thread-42"


def test_deferrals_are_written_with_the_plan(chart):
    """A plan that quietly addressed some problems would be
    indistinguishable from one that missed the rest."""
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    plans = Base.metadata.tables["care_plan_records"]
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    assert row.deferred["problems"] == ["Essential hypertension — controlled"]


def test_an_empty_plan_is_not_written(chart):
    session, patient = chart
    decision = persist_reviewed_plan(session, patient, {"concerns": []})
    assert not decision and "no concerns" in decision.detail


# ── the decision ─────────────────────────────────────────────────────────


def test_approval_is_recorded(chart):
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    assert decide(session, plan_id, True, "reviewed with the patient")
    plans = Base.metadata.tables["care_plan_records"]
    assert session.execute(select(plans).where(plans.c.id == plan_id)).first().status == APPROVED


def test_a_rejection_needs_a_reason(chart):
    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    decision = decide(session, plan_id, False, "   ")
    assert not decision and "needs a reason" in decision.detail


def test_a_rejection_keeps_its_reason_on_the_row(chart):
    from sqlalchemy import select

    from hdh.core.models import Base

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    decide(session, plan_id, False, "burden too high for this patient")
    plans = Base.metadata.tables["care_plan_records"]
    row = session.execute(select(plans).where(plans.c.id == plan_id)).first()
    assert row.status == REJECTED
    assert "burden too high" in row.rejection_reason


def test_a_plan_is_decided_once(chart):
    """A second decision means something upstream lost track; allowing it
    silently would hide that."""
    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    decide(session, plan_id, True, "signed off")
    again = decide(session, plan_id, False, "changed my mind")
    assert not again and "already approved" in again.detail


def test_deciding_a_plan_that_does_not_exist_says_so(chart):
    session, _patient = chart
    assert not decide(session, 9999, True, "")


# ── the trail ────────────────────────────────────────────────────────────


def test_writing_and_deciding_both_leave_a_trail(chart):
    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    decide(session, plan_id, True, "reviewed with the patient")

    events = history(session, plan_id)
    assert [e["action"] for e in events] == ["create", "approve"]
    assert events[0]["after"]["concerns"] == 2
    assert events[1]["before"]["status"] == USER_EDITED
    assert events[1]["after"]["status"] == APPROVED
    assert "reviewed with the patient" in events[1]["reason"]


def test_the_trail_shares_the_chart_audit_table(chart):
    """One question — who changed this, when, why — so one trail. Two would
    mean two places to look and two chances to look in the wrong one."""
    from sqlalchemy import select

    from hdh.core.models import ChartAuditEvent

    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    rows = session.execute(select(ChartAuditEvent).where(ChartAuditEvent.entity == ENTITY)).scalars().all()
    assert rows and rows[0].row_id == plan_id
    assert rows[0].patient_id == patient.id


def test_an_approval_without_a_comment_still_records_something(chart):
    """An approval nobody can audit is worth little more than a refusal
    nobody can check."""
    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    decide(session, plan_id, True)
    assert history(session, plan_id)[-1]["reason"]


def test_the_latest_plan_is_findable(chart):
    session, patient = chart
    first = persist_reviewed_plan(session, patient, _values()).plan_id
    second = persist_reviewed_plan(session, patient, _values()).plan_id
    assert latest_plan_id(session, patient.id) == second != first
