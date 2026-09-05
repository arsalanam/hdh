"""A person has more than one of most things (M3).

The chart held exactly one identifier, one address, one phone, one insurer —
as columns on `patients` — so a national number beside the MRN, a former
address, a mobile beside a landline, or a secondary payer were all
unrecordable, and the answer to each was going to be another column.

Also here: the third incarnation of a bug this codebase has now hit three
times. `patient.sex` is **not reliably** the `Sex` enum. A freshly built
instance carries `Sex.FEMALE`; one reloaded after a flush carries the raw
column value `'F'`. Both are `str` subclasses, so neither raises on
`str(...)` and the difference goes unnoticed — until:

  - the FHIR emitter tested `str(sex).endswith("FEMALE")`, right for
    `'Sex.FEMALE'` and wrong for `'F'`, and exported **female patients as
    male** on every path that produced a raw string;
  - `patient_to_text` called `sex.label`, which raises outright on `'F'`,
    crashing the chart the agent reads.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.models import Sex


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=6, years_of_history=2, verbose=False, seed=5, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


# ── Sex, compared by identity and never by substring ─────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (Sex.FEMALE, Sex.FEMALE),
        (Sex.MALE, Sex.MALE),
        ("F", Sex.FEMALE),
        ("M", Sex.MALE),
        ("FEMALE", Sex.FEMALE),
        ("male", Sex.MALE),
        ("Sex.FEMALE", Sex.FEMALE),
        (None, None),
        ("", None),
        ("unknown", None),
    ],
)
def test_sex_coerces_from_every_form_it_actually_arrives_in(value, expected):
    assert Sex.coerce(value) is expected


def test_the_raw_column_value_is_the_one_that_broke_things():
    """'F' ends in neither 'FEMALE' nor 'MALE', so every substring test ever
    written against it answers male."""
    assert "F".endswith("FEMALE") is False
    assert Sex.coerce("F") is Sex.FEMALE


# ── which is what the exporters must not get wrong ───────────────────────


def _patient_resource(patient) -> dict:
    from hdh.core.exporters import patient_to_fhir_bundle

    return next(
        entry["resource"]
        for entry in patient_to_fhir_bundle(patient)["entry"]
        if entry["resource"]["resourceType"] == "Patient"
    )


def test_every_generated_patient_exports_the_gender_it_was_given(chart):
    """Three of eight patients in a sample were exported male while stored
    female, because their sex arrived as a raw string."""
    from hdh.core.models import Patient

    session, _first = chart
    for patient in session.query(Patient):
        resolved = Sex.coerce(patient.sex)
        expected = "male" if resolved is not None and resolved.is_male else "female"
        assert _patient_resource(patient)["gender"] == expected, patient.mrn


def test_an_unrecordable_sex_exports_as_unknown_not_as_a_guess():
    """`unknown` is a legal FHIR value and the honest one. Defaulting to a
    sex is how this went wrong the first three times."""
    from hdh.core.fhir.emitters import _gender

    assert _gender(None) == "unknown"
    assert _gender("not-a-sex") == "unknown"


def test_the_text_chart_survives_a_raw_string_sex(chart):
    """`sex.label` raises on 'F'. The chart the agent reads crashed."""
    from hdh.core.exporters import patient_to_text
    from hdh.core.models import Patient

    session, _first = chart
    session.expire_all()  # force the reload path that yields raw strings
    for patient in session.query(Patient):
        assert "Sex:" in patient_to_text(patient)


# ── identifiers, addresses, contacts, coverage ───────────────────────────


def test_the_generator_writes_the_row_form_too(chart):
    """M3 keeps the flat columns as the primary value while readers move
    over. A generator writing only one of the two would produce charts where
    they disagree on day one."""
    _session, patient = chart
    assert patient.identifiers
    assert patient.addresses
    assert patient.contacts
    assert patient.coverages


def test_the_row_and_the_column_agree(chart):
    """The cost of the transitional duplication, gated so it cannot drift
    silently. When the flat columns go, this test goes with them."""
    from hdh.core.models import Patient

    session, _first = chart
    for patient in session.query(Patient):
        home = next((a for a in patient.addresses if a.use == "home"), None)
        assert home is not None and home.line == patient.address
        phone = next((c for c in patient.contacts if c.system == "phone"), None)
        assert phone is not None and phone.value == patient.phone
        assert patient.primary_coverage.payer_name == patient.insurance_name


def test_a_second_identifier_is_recordable(chart):
    """The thing a column could not do."""
    from hdh.core.models import PatientIdentifier

    session, patient = chart
    patient.identifiers.append(
        PatientIdentifier(kind="national", value="NHS-1234567890", issuer="NHS England")
    )
    session.commit()
    session.refresh(patient)
    kinds = {i.kind for i in patient.identifiers}
    assert kinds == {"mrn", "national"}


def test_secondary_coverage_is_recordable_and_ranked(chart):
    """Which payer is billed first is not a detail."""
    from hdh.core.models import PatientCoverage

    session, patient = chart
    patient.coverages.append(PatientCoverage(rank=2, payer_name="Second Payer", member_id="X1"))
    session.commit()
    session.refresh(patient)
    assert len(patient.coverages) == 2
    assert patient.primary_coverage.rank == 1


def test_primary_coverage_is_by_rank_not_insertion_order(chart):
    """Reading coverages[0] would eventually bill the wrong payer."""
    from hdh.core.models import PatientCoverage

    session, patient = chart
    patient.coverages.clear()
    session.commit()
    patient.coverages.append(PatientCoverage(rank=2, payer_name="Secondary"))
    patient.coverages.append(PatientCoverage(rank=1, payer_name="Primary"))
    session.commit()
    session.refresh(patient)
    assert patient.primary_coverage.payer_name == "Primary"


def test_a_former_address_keeps_its_end_date(chart):
    """An address with an end date is where they used to live, which is the
    difference between a stale chart and one that knows it is stale."""
    from hdh.core.models import PatientAddress

    session, patient = chart
    patient.addresses.append(PatientAddress(use="home", line="12 Old Road", period_end=date(2024, 6, 1)))
    session.commit()
    session.refresh(patient)
    former = [a for a in patient.addresses if a.period_end]
    assert former and former[0].line == "12 Old Road"


# ── the person, in the export ────────────────────────────────────────────


def test_every_identifier_reaches_the_fhir_resource(chart):
    from hdh.core.models import PatientIdentifier

    session, patient = chart
    patient.identifiers.append(PatientIdentifier(kind="national", value="NHS-999"))
    session.commit()
    session.refresh(patient)
    values = {i["value"] for i in _patient_resource(patient)["identifier"]}
    assert patient.mrn in values
    assert "NHS-999" in values


def test_the_mrn_is_emitted_even_with_no_identifier_rows(chart):
    """It is the chart's anchor and must survive a database whose rows were
    never backfilled."""
    session, patient = chart
    patient.identifiers.clear()
    session.commit()
    session.refresh(patient)
    values = {i["value"] for i in _patient_resource(patient)["identifier"]}
    assert patient.mrn in values


def test_a_preferred_name_is_exported_as_the_usual_name(chart):
    """FHIR has a `use` for exactly this. Emitting only the official name
    means every downstream letter uses it."""
    session, patient = chart
    patient.preferred_name = "Bea"
    session.commit()
    session.refresh(patient)
    names = _patient_resource(patient)["name"]
    assert {n["use"] for n in names} == {"official", "usual"}
    assert any("Bea" in n["given"] for n in names)


def test_no_usual_name_when_there_is_no_preferred_one(chart):
    """An empty preferred name is the ordinary case, not a second name."""
    _session, patient = chart
    assert [n["use"] for n in _patient_resource(patient)["name"]] == ["official"]


def test_contacts_are_exported_in_rank_order(chart):
    """Which number to try first is the whole question at the point of use."""
    _session, patient = chart
    telecom = _patient_resource(patient)["telecom"]
    ranks = [t.get("rank") for t in telecom if t.get("rank")]
    assert ranks == sorted(ranks)


def test_display_name_prefers_what_they_answer_to(chart):
    session, patient = chart
    assert patient.display_name.startswith(patient.first_name)
    patient.preferred_name = "Bea"
    assert patient.display_name.startswith("Bea")
