"""The eval harness: the parts that need no database and no model.

The harness exists because three separate decisions this month ended at
*"we cannot tell without an eval set"* — whether semantic retrieval would
help, what the fan-out constants should be, and whether the revise loop
improves plans. What makes it worth trusting is not that it produces a
number; it is that the number means the same thing twice.

So these tests are about reproducibility and about the one judgement the
harness makes on its own: whether a change is distinguishable from noise.
"""

from __future__ import annotations

import json

import pytest

from hdh.modules.careplan.evalset import (
    Cohort,
    EvalError,
    Measurement,
    Report,
    Stratum,
    compare,
    load_cohort,
    save_baseline,
)


def _measurement(mrn: str, means: list[float | None], stratum: str = "typical") -> Measurement:
    return Measurement(
        mrn=mrn,
        stratum=stratum,
        rubric="default",
        runs=[{"mean": value} for value in means],
    )


# ── the cohort spec ──────────────────────────────────────────────────────


def test_the_bundled_cohort_loads():
    cohort = load_cohort()
    assert cohort.seed and cohort.patients > cohort.case_count
    assert {stratum.name for stratum in cohort.strata} == {"single", "typical", "multi", "complex"}


def test_the_pool_is_larger_than_the_case_count():
    """Strata are defined by topic count, and a pool the size of the case
    count would not reliably contain the complex tail — which is 9% of the
    population, so eight patients would often contain none."""
    cohort = load_cohort()
    assert cohort.patients >= 5 * cohort.case_count


def test_strata_cover_the_range_without_gaps():
    """A patient whose topic count falls in no stratum is silently
    unselectable, and nothing would say so."""
    cohort = load_cohort()
    ordered = sorted(cohort.strata, key=lambda stratum: stratum.min_topics)
    assert ordered[0].min_topics == 1
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert later.min_topics == earlier.max_topics + 1, "gap between strata"
    assert ordered[-1].max_topics > 100, "the top stratum must be open-ended"


def test_a_missing_cohort_says_so():
    with pytest.raises(EvalError, match="no cohort spec"):
        load_cohort("no-such-cohort")


def test_a_spec_missing_a_field_does_not_load(tmp_path):
    (tmp_path / "cohort.json").write_text(json.dumps({"name": "x", "version": 1}), encoding="utf-8")
    with pytest.raises(EvalError, match="missing"):
        load_cohort(root=tmp_path)


def test_a_stratum_matches_only_its_own_range():
    stratum = Stratum(name="typical", min_topics=3, max_topics=4, take=2)
    assert stratum.holds(3) and stratum.holds(4)
    assert not stratum.holds(2) and not stratum.holds(5)


# ── noise, and the judgement the harness makes ───────────────────────────


def test_spread_is_zero_for_a_single_run():
    """One run per case measures no noise floor, and reporting 0.0 is
    honest about that — it is not a claim that the process is stable."""
    assert _measurement("A", [4.0]).spread == 0.0


def test_spread_is_the_observed_range():
    assert _measurement("A", [3.33, 3.67]).spread == pytest.approx(0.34)


def test_a_change_inside_the_noise_is_reported_as_indistinguishable():
    """The whole reason this module exists.

    M3c raised a plan's mean by 0.17 while run-to-run variance on the same
    chart was 0.34. A harness that printed "+0.17, improved" would have
    been reporting noise as a result, and the temptation to believe it is
    exactly why the judgement is made here rather than by whoever reads
    the output.
    """
    report = Report(cohort="default", measurements=[_measurement("A", [3.33, 3.67])])
    baseline = {"mean": 3.33, "noise": 0.34, "cases": [{"mrn": "A", "mean": 3.33}]}
    lines = compare(report, baseline)
    assert any("NOT DISTINGUISHABLE" in line for line in lines)


def test_a_change_beyond_the_noise_is_reported_as_real():
    report = Report(cohort="default", measurements=[_measurement("A", [4.5, 4.5])])
    baseline = {"mean": 3.0, "noise": 0.1, "cases": [{"mrn": "A", "mean": 3.0}]}
    lines = compare(report, baseline)
    assert not any("NOT DISTINGUISHABLE" in line for line in lines)
    assert any("outside the observed spread" in line for line in lines)


def test_a_run_with_no_repeats_says_it_has_no_noise_floor():
    """Without repeats there is nothing to compare a delta against, and
    claiming an improvement would be claiming more than was measured."""
    report = Report(cohort="default", measurements=[_measurement("A", [4.0])])
    baseline = {"mean": 3.0, "noise": 0.0, "cases": [{"mrn": "A", "mean": 3.0}]}
    lines = compare(report, baseline)
    assert any("no noise floor" in line for line in lines)


def test_the_noise_floor_is_the_worst_case_not_the_average():
    """A delta has to beat the noisiest case, because that is the case that
    could have produced it by itself."""
    report = Report(
        cohort="default",
        measurements=[_measurement("A", [4.0, 4.0]), _measurement("B", [3.0, 4.0])],
    )
    assert report.noise == pytest.approx(1.0)


def test_a_case_with_no_baseline_is_reported_rather_than_dropped():
    report = Report(cohort="default", measurements=[_measurement("NEW", [4.0])])
    lines = compare(report, {"mean": 3.0, "noise": 0.0, "cases": []})
    assert any("no baseline" in line for line in lines)


def test_an_ungraded_run_does_not_become_a_zero():
    """The same rule the grader follows: an unknown is not a low score."""
    measurement = _measurement("A", [None, 4.0])
    assert measurement.mean == 4.0


def test_a_case_where_nothing_graded_has_no_mean():
    assert _measurement("A", [None, None]).mean is None


# ── the baseline round-trips ─────────────────────────────────────────────


def test_a_report_saves_and_reloads_unchanged(tmp_path):
    """A baseline that cannot be read back is not a baseline."""
    from hdh.modules.careplan.evalset import load_baseline

    report = Report(
        cohort="default",
        measurements=[_measurement("A", [3.5, 3.6]), _measurement("B", [4.0], "complex")],
    )
    path = tmp_path / "baseline.json"
    save_baseline(path, report)
    restored = load_baseline(path)
    assert restored["mean"] == report.mean
    assert restored["noise"] == report.noise
    assert {case["mrn"] for case in restored["cases"]} == {"A", "B"}


def test_a_missing_baseline_names_the_fix():
    from hdh.modules.careplan.evalset import load_baseline

    with pytest.raises(EvalError, match="--save"):
        load_baseline(load_cohort.__globals__["HERE"] / "no-such-baseline.json")


def test_a_cohort_is_immutable():
    """Two runs of the harness must mean the same thing, which they cannot
    if the spec can be edited underneath them."""
    from dataclasses import FrozenInstanceError

    cohort = load_cohort()
    assert isinstance(cohort, Cohort)
    with pytest.raises(FrozenInstanceError):
        cohort.seed = 1  # type: ignore[misc]
