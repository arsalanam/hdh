"""A finished course is not an active medication (#115).

The generator has always known this; the two modules that *read* the chart
did not. Both took every prescription written in the last 365 days, so a
five-day antibiotic finished in March counted as an active medication in
December.

Measured on the eval cohort: **44% of "active" medications were finished
courses** (210 -> 117). For the patient the care-plan review used, the list
went 10 -> 7, and the three dropped were exactly the short courses — which
matters because that plan's flagship safety intervention was "discontinue
ibuprofen and naproxen" for a triple-NSAID overlap the patient was not
actually in.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hdh.core.medications import ONGOING_WINDOW_DAYS, is_current, is_current_row

TODAY = date(2026, 8, 27)


# ── the rule ─────────────────────────────────────────────────────────────


def test_a_course_is_current_while_it_runs():
    assert is_current(TODAY - timedelta(days=3), 7, TODAY)


def test_a_course_is_not_current_once_it_has_finished():
    """The whole of #115 in one assertion."""
    assert not is_current(TODAY - timedelta(days=60), 7, TODAY)


def test_a_course_is_current_on_its_last_day():
    assert is_current(TODAY - timedelta(days=7), 7, TODAY)


def test_a_prescription_with_no_duration_falls_back_to_the_window():
    """An ongoing repeat has no end date, so it needs one imposed — a
    prescription written two years ago and never renewed is not evidence the
    patient is still taking it either."""
    assert is_current(TODAY - timedelta(days=300), None, TODAY)
    assert not is_current(TODAY - timedelta(days=400), None, TODAY)


def test_a_long_course_outlives_the_window():
    """The case a visit-level date filter got wrong in the other direction:
    a two-year course written 400 days ago is still running."""
    assert is_current(TODAY - timedelta(days=400), 730, TODAY)


def test_a_prescription_with_no_start_date_is_not_current():
    """It cannot support a claim about now, and guessing would put an
    unknowable into a list that gets reasoned over."""
    assert not is_current(None, 7, TODAY)


def test_a_future_dated_prescription_is_not_current_yet():
    assert not is_current(TODAY + timedelta(days=5), None, TODAY)


def test_the_window_is_configurable_but_shared_by_default():
    assert ONGOING_WINDOW_DAYS == 365
    assert is_current(TODAY - timedelta(days=400), None, TODAY, window_days=500)


@pytest.mark.parametrize("shape", ["dict", "row"])
def test_the_row_helper_reads_either_shape(shape):
    """The generator holds dicts; the readers hold ORM rows. One rule."""

    class _Row:
        duration_days = 7

    prescription = {"duration_days": 7} if shape == "dict" else _Row()
    assert is_current_row(prescription, TODAY, started=TODAY - timedelta(days=2))
    assert not is_current_row(prescription, TODAY, started=TODAY - timedelta(days=60))


# ── the three callers agree, because there is one rule ───────────────────


def test_the_generator_delegates_rather_than_keeping_a_second_copy():
    """The copies drifted once; that is what #115 was."""
    from hdh.core.generators import _prescription_is_current

    started = TODAY - timedelta(days=60)
    assert not _prescription_is_current(started, {"duration_days": 7}, TODAY)
    assert _prescription_is_current(started, {"duration_days": None}, TODAY)


def test_careplan_reexports_the_shared_window():
    """A restated constant is how two modules came to agree on the window
    while disagreeing about whether a finished course counts at all."""
    from hdh.modules.careplan.context import MEDICATION_WINDOW_DAYS

    assert MEDICATION_WINDOW_DAYS == ONGOING_WINDOW_DAYS


# ── end to end, on a generated chart ─────────────────────────────────────


@pytest.fixture(scope="module")
def chart(tmp_path_factory):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("meds") / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(
        session, n_patients=20, years_of_history=3, verbose=False, seed=4242, as_of=date(2026, 8, 14)
    )
    yield session
    session.close()
    engine.dispose()


def test_no_finished_course_reaches_a_care_plan(chart):
    """The property the fix exists for, checked against real generated data
    rather than a constructed row."""
    from hdh.core.models import Patient
    from hdh.modules.caregaps import reference_date
    from hdh.modules.careplan.context import build_context

    as_of = reference_date(chart)
    checked = 0
    for patient in chart.query(Patient).all():
        context = build_context(chart, patient)
        if not context.medications:
            continue
        by_name = {}
        for visit in patient.visits:
            for rx in visit.prescriptions:
                by_name.setdefault((rx.drug_name or "").lower(), []).append(
                    (visit.visit_date, rx.duration_days)
                )
        for medication in context.medications:
            written = by_name.get(medication.name.lower(), [])
            assert any(is_current(started, duration, as_of) for started, duration in written), (
                f"{medication.name} is on {patient.mrn}'s active list with no running prescription"
            )
            checked += 1
    assert checked, "the fixture produced no medications to check"


def test_the_active_list_is_shorter_than_everything_written(chart):
    """If these were equal the filter would not be doing anything — the
    shape of a vacuous pass, which this project has been bitten by."""
    from hdh.core.models import Patient
    from hdh.modules.caregaps import reference_date
    from hdh.modules.careplan.context import build_context

    as_of = reference_date(chart)
    window_start = as_of - timedelta(days=ONGOING_WINDOW_DAYS)
    active = written = 0
    for patient in chart.query(Patient).all():
        active += len(build_context(chart, patient).medications)
        written += len(
            {
                (rx.drug_name or "").lower()
                for visit in patient.visits
                if visit.visit_date >= window_start
                for rx in visit.prescriptions
            }
        )
    assert written > active, f"the duration filter dropped nothing ({written} written, {active} active)"
