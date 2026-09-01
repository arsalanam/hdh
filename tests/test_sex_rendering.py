""" "MALE" is a substring of "FEMALE", and two call sites tested for it.

Found by the agent, of all things: asked to build a care plan it reported a
sex/name discrepancy — "Jennifer Webb" with "Sex: Male" — and the validator
agreed with it. Both had read the chart correctly. The chart was wrong.

The bug had two homes and neither announced itself: `patient_to_text`
rendered *every* patient as Male, in the text the agent reads and reasons
over, and the risk model encoded every patient as male, so its sex feature
was a constant carrying no information.
"""

from __future__ import annotations

from hdh.core.models import Sex


def test_the_substring_that_caused_this_really_is_a_substring():
    """The whole bug in one assertion. If this ever becomes False the enum
    was renamed and these tests need rereading, not deleting."""
    assert "MALE" in str(Sex.FEMALE)


def test_sex_is_decided_by_identity_not_by_substring():
    assert Sex.MALE.is_male
    assert not Sex.FEMALE.is_male


def test_labels_are_right_for_both():
    assert Sex.MALE.label == "Male"
    assert Sex.FEMALE.label == "Female"


def test_the_chart_the_agent_reads_shows_the_right_sex():
    """The rendering that actually misled a model.

    Both sexes are constructed rather than drawn from a generated fixture:
    the bug only shows on a FEMALE patient, and a fixture that happened to
    contain none would pass while the bug was still there.
    """
    from datetime import date

    from hdh.core.exporters import patient_to_text
    from hdh.core.models import Patient

    for sex, expected in ((Sex.FEMALE, "Female"), (Sex.MALE, "Male")):
        patient = Patient(
            mrn=f"MRN-{expected}",
            first_name="A",
            last_name="B",
            date_of_birth=date(1950, 1, 1),
            sex=sex,
        )
        line = next(row for row in patient_to_text(patient).splitlines() if "Sex:" in row)
        rendered = line.split("Sex:")[1].strip()
        assert rendered == expected, f"{sex} rendered as {rendered}"


def test_the_risk_model_does_not_encode_everyone_as_male(db_session):
    """A feature that is constant across the cohort is not a feature. This
    one looked plausible for as long as nobody summed it."""
    from hdh.modules.caregaps import reference_date
    from hdh.modules.risk.features import FEATURE_NAMES, extract_features

    _mrns, rows, _y = extract_features(db_session, cutoff=reference_date(db_session), with_labels=False)
    assert rows, "no feature rows — this test would be vacuous"

    index = FEATURE_NAMES.index("sex_male")
    encoded = {row[index] for row in rows}
    assert len(encoded) > 1, (
        f"every patient encoded as sex_male={encoded.pop()} — a constant feature "
        f"carries no information, which is what the substring test produced"
    )
