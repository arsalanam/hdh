"""The chart is complete, and the next module cannot quietly undo it (M6).

Four tables — allergies, immunizations, procedures, family_history — were
generated, populated, and reachable by no route the agent had. Nothing
failed. `immunizations` and `procedures` appeared zero times in the chart
text; an allergy question routed to `patient_lookup`, where the SQL tool
could not see the table, while the chart summary said `NKDA`.

They drifted in the only way they could: each new module ships a table
without shipping the decision about whether it belongs to the *person* or to
one *episode of care*. So a table that hangs off a patient must now say
which side of that boundary it is on, and saying nothing is what fails.

The rule (docs/design/patient-chart-completeness.md §3):

    The chart holds what is true of the person between encounters.
    A module holds what is true of one episode of care.
"""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()


# ── the gate ─────────────────────────────────────────────────────────────


def test_every_patient_scoped_table_declares_which_side_it_is_on():
    """Saying nothing is what fails. A new module's table joins the chart by
    decision or stays out by decision — never by nobody noticing."""
    from hdh.core.schema_registry import undeclared_chart_tables

    undeclared = sorted(undeclared_chart_tables())
    assert not undeclared, (
        "these tables hang off a patient and have not declared `chart: true|false` "
        f"in their semantics block: {undeclared}. See docs/design/"
        "patient-chart-completeness.md §3 — does it outlive the episode?"
    )


def test_every_chart_table_is_reachable_by_the_agent():
    """The failure this exists to stop. A chart table no intent exposes is
    data the agent cannot reach, and its absence reads as absence of fact."""
    from hdh.core.schema_registry import chart_tables
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    exposed = {table for tables in INTENT_TABLES.values() for table in tables}
    unreachable = sorted(chart_tables() - exposed)
    assert not unreachable, f"chart tables no intent exposes: {unreachable}"


def test_every_chart_table_declares_what_it_means():
    """#93's gate covers what intents expose; this covers what the chart IS,
    which is the set that must never go bare."""
    from hdh.core.schema_registry import chart_tables, table_semantics

    meanings = table_semantics()
    silent = sorted(t for t in chart_tables() if not meanings.get(t, {}).get("purpose"))
    assert not silent, f"chart tables with no declared purpose: {silent}"


def test_a_table_kept_out_of_the_chart_says_why():
    """An unexplained exclusion is indistinguishable from an oversight, and
    the next reader has to re-derive the argument."""
    from hdh.core.schema_registry import table_semantics

    unexplained = sorted(
        name
        for name, block in table_semantics().items()
        if block.get("chart") is False and not block.get("not_chart_because")
    )
    assert not unexplained, f"excluded from the chart with no reason given: {unexplained}"


# ── the boundary itself ──────────────────────────────────────────────────


def test_a_care_plan_is_not_chart():
    """The worked example of the rule. A care plan is a module's episode of
    work; what it PRODUCES — a procedure, an allergy, a diagnosis, a change
    in function — lands in the chart. The plan does not."""
    from hdh.core.schema_registry import chart_tables

    chart = chart_tables()
    for table in ("care_plan_records", "health_concerns", "plan_goals", "plan_interventions"):
        assert table not in chart, f"{table} is a care plan's own workings, not the person's chart"


def test_the_things_a_care_plan_produces_ARE_chart():
    """The other half. If none of these were chart, the boundary would be
    drawn so that a module contributes nothing back."""
    from hdh.core.schema_registry import chart_tables

    chart = chart_tables()
    for table in ("procedures", "allergies", "conditions", "functional_status"):
        assert table in chart


def test_directories_and_catalogues_are_not_chart():
    """`medications` is the formulary — a catalogue of drugs that exist, not
    a fact about any patient. Counting it as chart would make every
    completeness statement meaningless."""
    from hdh.core.schema_registry import chart_tables

    chart = chart_tables()
    for table in ("medications", "providers", "specialties", "knowledge_chunks"):
        assert table not in chart


def test_the_audit_trail_is_not_chart_content():
    """It is the trail OF changes to the chart. Including it would mean the
    chart contains its own history of being edited."""
    from hdh.core.schema_registry import chart_tables

    assert "chart_audit_events" not in chart_tables()


# ── what the chart actually shows ────────────────────────────────────────


@pytest.fixture()
def patient(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session

    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=4, years_of_history=3, verbose=False, seed=21, as_of=date(2026, 8, 14))
    row = session.query(Patient).first()
    yield session, row
    session.close()
    engine.dispose()


def test_the_chart_renders_a_section_for_every_kind_of_content_it_holds(patient):
    """Not every table — a patient with no procedures gets no PROCEDURES
    heading, because an empty heading reads as "checked, none". This asserts
    the sections exist at all, which is what was missing."""
    import inspect

    from hdh.core import exporters

    source = inspect.getsource(exporters.patient_to_text)
    for section in ("_allergy_line", "_append_function", "_append_immunizations", "_append_procedures"):
        assert section in source, f"{section} is not part of the chart text"
