"""The chart shows what it knows, and says so when it knows nothing (M1/M2).

Measured before this existed, against the working database:

  - 36 of 60 sampled patients rendered `Allergies: NKDA` from having no
    allergy rows. Absence meaning "none" is this chart's contract — see
    `models.Allergy` — but the line asserted it in the vocabulary of a
    clinician who had asked.
  - `Aspirin / GI upset / MILD` rendered as `Aspirin`. Anaphylaxis and a
    mild rash were the same string to the agent, from a row holding the
    difference.
  - `immunizations` and `procedures` appeared ZERO times in the chart text,
    and neither they nor `allergies` nor `family_history` were exposed by
    any intent. The data was generated, populated, and unreachable by any
    route the agent had.
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
    build_dataset(session, n_patients=6, years_of_history=3, verbose=False, seed=7, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _text(patient) -> str:
    from hdh.core.exporters import patient_to_text

    return patient_to_text(patient)


def _allergy_line(patient) -> str:
    return next(line for line in _text(patient).splitlines() if line.startswith("Allergies"))


def _add_allergy(session, patient, **kw):
    from hdh.core.models import Allergy

    allergy = Allergy(patient_id=patient.id, **kw)
    session.add(allergy)
    session.commit()
    session.refresh(patient)
    return allergy


# ── what an allergy row says ─────────────────────────────────────────────


def test_severity_and_reaction_reach_the_page(chart):
    """They were in the row and not on the page."""
    from hdh.core.models import AllergySeverity

    session, patient = chart
    _add_allergy(
        session, patient, substance="Penicillin", reaction="anaphylaxis", severity=AllergySeverity.SEVERE
    )
    line = _allergy_line(patient)
    assert "Penicillin" in line
    assert "severe" in line
    assert "anaphylaxis" in line


def test_two_allergies_of_different_severity_are_distinguishable(chart):
    """The whole point. Before this they rendered as the same string."""
    from hdh.core.models import AllergySeverity

    session, patient = chart
    _add_allergy(
        session, patient, substance="Penicillin", reaction="anaphylaxis", severity=AllergySeverity.SEVERE
    )
    _add_allergy(session, patient, substance="Aspirin", reaction="GI upset", severity=AllergySeverity.MILD)
    line = _allergy_line(patient)
    assert "Penicillin" in line and "anaphylaxis" in line
    assert "Aspirin" in line and "GI upset" in line


def test_a_coded_allergy_shows_which_vocabulary_it_came_from(chart):
    """A bare code cannot say whether it is RxNorm or SNOMED, and guessing
    from its shape is how a drug allergy becomes a food one."""
    session, patient = chart
    _add_allergy(session, patient, substance="Penicillin", drug_code="7980", code_standard="RxNorm")
    assert "RxNorm:7980" in _allergy_line(patient)


def test_an_uncoded_allergy_renders_without_pretending(chart):
    """NULL codes are common and are not a data error."""
    session, patient = chart
    _add_allergy(session, patient, substance="Shellfish")
    line = _allergy_line(patient)
    assert "Shellfish" in line
    assert "[" not in line


def test_an_empty_list_says_where_it_came_from(chart):
    """Absence means none — that is the contract. But the line now shows a
    reader it came from an empty list rather than from someone asking."""
    _session, patient = chart
    assert "none recorded" in _allergy_line(patient)


def test_a_voided_allergy_is_not_shown(chart):
    """Entered in error is not the same as resolved, and either way it must
    not appear as a live allergy."""
    session, patient = chart
    _add_allergy(session, patient, substance="Codeine", voided_at=datetime(2026, 1, 1))
    line = _allergy_line(patient)
    assert "Codeine" not in line
    assert "none recorded" in line


def test_last_happened_is_a_separate_question_from_noted_date(chart):
    """A severe reaction thirty years ago is weighed differently from one
    last month, and the schema has to be able to tell them apart."""
    session, patient = chart
    allergy = _add_allergy(
        session, patient, substance="Penicillin", noted_date=date(2026, 1, 1), last_happened=date(1994, 6, 2)
    )
    assert allergy.noted_date != allergy.last_happened


# ── the sections that were generated and never rendered ──────────────────


def test_immunisations_appear_in_the_chart(chart):
    from hdh.core.models import Immunization

    session, patient = chart
    session.add(
        Immunization(
            patient_id=patient.id,
            vaccine="Influenza, seasonal",
            cvx_code="141",
            administered_date=date(2025, 9, 10),
            dose_number=1,
        )
    )
    session.commit()
    session.refresh(patient)
    text = _text(patient)
    assert "IMMUNISATIONS" in text
    assert "Influenza" in text


def test_procedures_appear_in_the_chart(chart):
    from hdh.core.models import Procedure

    session, patient = chart
    session.add(
        Procedure(patient_id=patient.id, description="Splint application", performed_date=date(2025, 5, 8))
    )
    session.commit()
    session.refresh(patient)
    assert "Splint application" in _text(patient)


def test_a_procedure_with_no_visit_is_marked_as_history(chart):
    """`procedures.visit_id` has always been nullable so a past appendectomy
    can be recorded without inventing an encounter. Rendering it identically
    to something done here would misattribute it to this practice."""
    from hdh.core.models import Procedure

    session, patient = chart
    session.add(Procedure(patient_id=patient.id, description="Appendectomy", performed_date=date(1998, 3, 4)))
    session.commit()
    session.refresh(patient)
    text = _text(patient)
    assert "Appendectomy" in text
    assert "not performed here" in text


def test_a_patient_with_nothing_gets_no_empty_sections(chart):
    """An empty heading reads as 'checked, none' and costs tokens to say it."""
    _session, patient = chart
    assert "PROCEDURES" not in _text(patient)


# ── reachable by the agent, not only visible in the summary ──────────────


def test_the_four_unreachable_tables_are_now_exposed():
    """They were generated, populated, and in NO intent's table list. An
    allergy question routed to patient_lookup, where the SQL tool could not
    see the table — while the chart summary said NKDA."""
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    exposed = {table for tables in INTENT_TABLES.values() for table in tables}
    for table in ("allergies", "immunizations", "procedures", "family_history"):
        assert table in exposed, f"{table} is still unreachable by every intent"


def test_a_prescribing_question_can_see_allergies():
    """The single most important pairing in this file."""
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    assert "allergies" in INTENT_TABLES["medication"]


def test_a_care_gap_question_can_see_immunisations():
    """Immunisation status IS a care gap."""
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    assert "immunizations" in INTENT_TABLES["care_gaps"]


def test_each_of_them_declares_what_it_means():
    """#93's gate covers this automatically; asserted here too because these
    four are the reason the gate exists."""
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    meanings = table_semantics()
    for table in ("allergies", "immunizations", "procedures", "family_history"):
        assert meanings.get(table, {}).get("purpose"), table


def test_the_agent_is_told_what_an_empty_allergy_list_means():
    """Without it the model re-derives the ambiguity we just removed."""
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    note = table_semantics()["allergies"]["use_when"]
    assert "no KNOWN allergies" in note
    assert "not asked" in note


def test_the_agent_is_warned_that_a_visitless_procedure_is_history():
    """Joining procedures to visits silently drops surgical history."""
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    assert "not performed here" in table_semantics()["procedures"]["columns"]["visit_id"]
