"""Codes, refusals, and the three ways an allergy stops being live (M5).

Three changes, one theme: the chart could describe these things and could
not say the part another clinician needs.

  - a procedure had free text and nothing codeable, and `laterality` — the
    field that makes a wrong-side procedure detectable — was not a field;
  - a declined vaccine was unrecordable, so its absence looked identical to
    never having been offered;
  - an allergy could be voided but not refuted, resolved, or marked high
    criticality with a mild reaction, which is the combination that matters
    most (a rash to penicillin can still be the one that kills them).
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
    build_dataset(session, n_patients=4, years_of_history=2, verbose=False, seed=17, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _text(patient) -> str:
    from hdh.core.exporters import patient_to_text

    return patient_to_text(patient)


def _allergy_line(patient) -> str:
    return next(line for line in _text(patient).splitlines() if line.startswith("Allergies"))


def _clear_allergies(session, patient):
    """This seed's patient already has allergies; these tests are about what
    a specific row does, not about the generated ones."""
    patient.allergies.clear()
    session.commit()
    session.refresh(patient)


def _add(session, patient, model, **kw):
    session.add(model(patient_id=patient.id, **kw))
    session.commit()
    session.refresh(patient)


# ── an allergy stops being live in three different ways ──────────────────


def test_a_refuted_allergy_is_not_live(chart):
    """It was investigated and found not to exist. It stays on the record so
    nobody re-adds it, and must never be rendered as an allergy."""
    from hdh.core.models import Allergy

    session, patient = chart
    _clear_allergies(session, patient)
    _add(session, patient, Allergy, substance="Penicillin", verification="refuted")
    line = _allergy_line(patient)
    assert "Penicillin" not in line
    assert "none recorded" in line


def test_a_resolved_allergy_is_not_live(chart):
    """Outgrown allergies are real."""
    from hdh.core.models import Allergy

    session, patient = chart
    _add(session, patient, Allergy, substance="Egg", clinical_status="resolved")
    assert "Egg" not in _allergy_line(patient)


def test_the_three_ways_are_distinct(chart):
    """Voided, refuted and resolved mean different things and are recorded
    separately — collapsing them loses why the row is not live."""
    from hdh.core.models import Allergy

    session, patient = chart
    _clear_allergies(session, patient)
    _add(session, patient, Allergy, substance="A", voided_at=datetime(2026, 1, 1))
    _add(session, patient, Allergy, substance="B", verification="refuted")
    _add(session, patient, Allergy, substance="C", clinical_status="resolved")
    _add(session, patient, Allergy, substance="D")
    assert len(patient.allergies) == 4
    line = _allergy_line(patient)
    assert "D" in line
    for gone in ("A", "B", "C"):
        assert f"{gone} " not in line and not line.endswith(gone)


def test_criticality_is_shown_and_is_not_severity(chart):
    """The clinically important distinction: severity is how bad the last
    reaction was, criticality is the risk the next one kills them."""
    from hdh.core.models import Allergy, AllergySeverity

    session, patient = chart
    _add(
        session,
        patient,
        Allergy,
        substance="Penicillin",
        severity=AllergySeverity.MILD,
        reaction="rash",
        criticality="high",
    )
    line = _allergy_line(patient)
    assert "high criticality" in line
    assert "mild" in line, "severity must survive alongside it"


def test_an_unconfirmed_allergy_is_marked_as_such(chart):
    """It is live — you prescribe around it — but a reader is entitled to
    know nobody has confirmed it."""
    from hdh.core.models import Allergy

    session, patient = chart
    _add(session, patient, Allergy, substance="Sulfa", verification="unconfirmed")
    line = _allergy_line(patient)
    assert "Sulfa" in line
    assert "UNCONFIRMED" in line


def test_rows_predating_these_columns_stay_live(chart):
    """NULL clinical_status means active — otherwise this migration would
    silently retire every allergy already on the chart."""
    from hdh.core.models import Allergy

    session, patient = chart
    _add(session, patient, Allergy, substance="Latex", clinical_status=None)
    assert "Latex" in _allergy_line(patient)


# ── a refused vaccine is a clinical fact ─────────────────────────────────


def test_a_refusal_is_recordable_and_shown(chart):
    """Previously unrecordable, and its absence looked identical to never
    having been offered."""
    from hdh.core.models import Immunization

    session, patient = chart
    _add(
        session,
        patient,
        Immunization,
        vaccine="Influenza, seasonal",
        status="refused",
        reason="declined, prefers not to",
        recorded_date=date(2026, 10, 1),
    )
    text = _text(patient)
    assert "REFUSED" in text
    assert "declined, prefers not to" in text


def test_a_refusal_needs_no_administration_date(chart):
    """Inventing one to satisfy a constraint records a dose never given."""
    from hdh.core.models import Immunization

    session, patient = chart
    _add(
        session, patient, Immunization, vaccine="Zoster", status="contraindicated", reason="immunosuppressed"
    )
    given = [i for i in patient.immunizations if i.administered_date is None]
    assert given and given[0].status == "contraindicated"


def test_an_administered_dose_still_reads_as_one(chart):
    from hdh.core.models import Immunization

    session, patient = chart
    _add(
        session,
        patient,
        Immunization,
        vaccine="Tdap",
        cvx_code="115",
        administered_date=date(2025, 4, 2),
        status="completed",
        dose_number=1,
    )
    text = _text(patient)
    assert "Tdap" in text and "CVX 115" in text
    assert "REFUSED" not in text


# ── a procedure that another system can act on ───────────────────────────


def test_a_procedure_carries_its_code_site_and_side(chart):
    """Laterality is its own column because it is what makes a wrong-side
    procedure detectable; in free text it is unqueryable."""
    from hdh.core.models import Procedure

    session, patient = chart
    _add(
        session,
        patient,
        Procedure,
        description="Total knee replacement",
        code="609588000",
        code_standard="SNOMED",
        body_site="knee",
        laterality="left",
        performed_date=date(2019, 7, 4),
    )
    text = _text(patient)
    assert "SNOMED:609588000" in text
    assert "knee" in text
    assert "(left)" in text


def test_surgical_history_still_reads_as_history(chart):
    """M2's rule survives the new columns: a procedure with no visit was not
    performed here."""
    from hdh.core.models import Procedure

    session, patient = chart
    _add(session, patient, Procedure, description="Appendectomy", performed_date=date(1998, 3, 4))
    assert "not performed here" in _text(patient)


# ── and the agent is told what the new columns mean ──────────────────────


def test_the_agent_is_warned_that_criticality_is_not_severity():
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    note = table_semantics()["allergies"]["columns"]["criticality"]
    assert "NOT severity" in note


def test_the_agent_is_told_a_refuted_allergy_is_not_live():
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    note = table_semantics()["allergies"]["columns"]["verification"]
    assert "never treat it as live" in note


def test_the_agent_is_warned_that_immunisation_rows_are_no_longer_all_doses():
    """Counting rows as doses given was right yesterday and is wrong now."""
    from hdh.core.schema_registry import bootstrap_schema, table_semantics

    bootstrap_schema()
    note = table_semantics()["immunizations"]["use_when"]
    assert "Counting rows as doses given is wrong" in note
