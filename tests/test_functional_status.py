"""What a person can actually do, and what its absence means (M4).

The care-plan rubric grades `feasibility_burden` — *"Could this patient
actually carry out this plan?"* — and until now the only facts it had were
`intervention_count`, `burden_limit`, `burden_flagged` and `bare_goals`.
All counts. Four interventions for someone who cannot leave the house scored
identically to four for someone who drives, because nothing in the chart
said which this was.

The contract here is the **opposite** of the allergy one, and the difference
is the point:

  - no allergy rows means no known allergies, because the chart tools always
    ask;
  - no functional-status row means **nobody asked**, because nothing does —
    so silence is ignorance, never independence.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=5, years_of_history=2, verbose=False, seed=9, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _record(session, patient, **kw):
    from hdh.core.models import FunctionalStatus

    session.add(FunctionalStatus(patient_id=patient.id, **kw))
    session.commit()
    session.refresh(patient)


def _context(session, patient):
    from hdh.modules.careplan.context import build_context

    return build_context(session, patient)


# ── absence is unassessed, not normal ────────────────────────────────────


def test_nothing_recorded_means_unassessed(chart):
    session, patient = chart
    social = _context(session, patient).social
    assert social.function == {}
    assert social.function_assessed is False


def test_unassessed_is_not_reported_as_independent(chart):
    """The whole distinction. A plan that reads silence as capability is
    understating what it is asking of someone."""
    session, patient = chart
    social = _context(session, patient).social
    assert social.needs_help == ()
    assert social.function_assessed is False, (
        "no domains needing help and no assessment are different states and must stay distinguishable"
    )


def test_an_empty_section_is_omitted_rather_than_printed(chart):
    """A heading with nothing under it reads as 'checked, fine'."""
    from hdh.core.exporters import patient_to_text

    _session, patient = chart
    assert "FUNCTIONAL STATUS" not in patient_to_text(patient)


# ── what a recorded assessment says ──────────────────────────────────────


def test_a_recorded_level_reaches_the_context(chart):
    session, patient = chart
    _record(session, patient, domain="mobility", level="assisted", aid="walking frame")
    social = _context(session, patient).social
    assert social.function["mobility"] == "assisted"
    assert social.function_assessed is True


def test_needs_help_names_only_the_domains_that_change_a_plan(chart):
    session, patient = chart
    _record(session, patient, domain="mobility", level="assisted")
    _record(session, patient, domain="vision", level="aided", aid="glasses")
    _record(session, patient, domain="adl", level="dependent")
    _record(session, patient, domain="hearing", level="independent")
    social = _context(session, patient).social
    assert social.needs_help == ("adl", "mobility")


def test_aids_already_in_use_are_collected(chart):
    """A plan proposing a walking frame to someone who has one is proposing
    nothing."""
    session, patient = chart
    _record(session, patient, domain="mobility", level="aided", aid="walking frame")
    _record(session, patient, domain="hearing", level="aided", aid="hearing aid")
    assert _context(session, patient).social.aids == ("hearing aid", "walking frame")


def test_a_voided_assessment_does_not_count(chart):
    """An assessment entered in error is one that never happened, and must
    not leave the patient looking assessed."""
    session, patient = chart
    _record(session, patient, domain="mobility", level="dependent", voided_at=datetime(2026, 1, 1))
    social = _context(session, patient).social
    assert social.function == {}
    assert social.function_assessed is False


def test_the_chart_shows_the_level_the_aid_and_the_date(chart):
    from hdh.core.exporters import patient_to_text

    session, patient = chart
    _record(
        session,
        patient,
        domain="mobility",
        level="assisted",
        aid="walking frame",
        assessed_date=date(2026, 3, 1),
    )
    text = patient_to_text(patient)
    assert "FUNCTIONAL STATUS" in text
    assert "mobility" in text and "assisted" in text
    assert "walking frame" in text
    assert "2026-03-01" in text


# ── the consumer that was waiting ────────────────────────────────────────


def test_the_feasibility_dimension_now_receives_these_facts():
    """It asked whether a patient could carry out a plan and was given four
    counts."""
    from hdh.modules.careplan.rubric import load_rubrics

    for rubric in load_rubrics():
        dimension = next((d for d in rubric.dimensions if d.id == "feasibility_burden"), None)
        if dimension is None:
            continue
        assert {"function_assessed", "needs_help_with", "aids_in_use"} <= set(dimension.facts), (
            rubric.rubric_id
        )


def test_the_fact_says_that_false_means_unknown():
    """The grader has to be told, or it re-derives the ambiguity."""
    from hdh.modules.careplan.facts import FACTS

    assert "NOT independent" in FACTS["function_assessed"].describe


# ── reachable, and explained ─────────────────────────────────────────────


def test_the_agent_can_reach_functional_status():
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    exposed = {table for tables in INTENT_TABLES.values() for table in tables}
    assert "functional_status" in exposed
    assert "functional_status" in INTENT_TABLES["care_plan"]


def test_the_agent_is_warned_that_absence_is_not_independence():
    """The single most misleading inference available here, and the reverse
    of the rule it just learned for allergies."""
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    note = table_semantics()["functional_status"]["use_when"]
    assert "never assessed" in note
    assert "Do not infer independence" in note
