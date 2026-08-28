"""A fixed cohort, so "did this change help" becomes a question with an answer.

Every number reported for the care-plan pipeline so far came from a chart
somebody chose, on a dev database that gets regenerated. That is not a
baseline. Three separate decisions this month — whether semantic retrieval
would help (#100), what the fan-out constants should be (#105), whether the
revise loop improves plans (M3c) — all ended at *"we cannot tell without an
eval set"*.

Four things make this one worth trusting.

**The cohort is regenerated, not committed.** ``build_dataset`` is
deterministic under a seed — verified both ways, same seed reproducing and
a different seed diverging — so pinning the seed pins the patients without
keeping a dump in git.

**Cases are selected by rule, never by hand.** Strata span the topic counts
triage actually produces, and within a stratum cases are taken in MRN
order. The one time a chart was picked to run against, it was the single
most extreme patient in the database and every constant derived from it was
fitted to an outlier.

**Deterministic checks are assertions; grader scores are not.** What must
hold — every problem addressed or recorded as deferred, every AI element
carrying evidence, no orphans — is checked exactly and fails loudly. Scores
are a measurement of a non-deterministic process and are *tracked*, never
asserted, because a test that pins a score is a record of what a model said
once.

**Variance is reported, not hidden.** M3c improved a plan's mean by 0.17
while run-to-run variance on the same chart was at least 0.34 — the
improvement was inside the noise and a single before-and-after could not
say so. ``--repeat`` exists to make that visible: a delta smaller than the
observed spread is reported as indistinguishable, because it is.
"""

from __future__ import annotations

import json
import pathlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent

#: How many patients to generate before selecting cases from them. Larger
#: than the case count on purpose: the strata are defined by topic count,
#: and a small pool would not reliably contain the complex tail.
DEFAULT_COHORT = "default"


class EvalError(RuntimeError):
    """The cohort or the baseline is not usable."""


@dataclass(frozen=True)
class Stratum:
    name: str
    min_topics: int
    max_topics: int
    take: int

    def holds(self, topics: int) -> bool:
        return self.min_topics <= topics <= self.max_topics


@dataclass(frozen=True)
class Cohort:
    """How to rebuild the patients and which of them are the cases."""

    name: str
    version: int
    seed: int
    patients: int
    years_of_history: int
    strata: tuple[Stratum, ...]

    @property
    def case_count(self) -> int:
        return sum(stratum.take for stratum in self.strata)


@dataclass(frozen=True)
class Case:
    """One chart the harness measures, and its deterministic shape."""

    mrn: str
    stratum: str
    age: int
    problems: int
    medications: int
    flags: int
    topics: int
    deferred: int
    rubric: str

    def as_dict(self) -> dict:
        """The case as JSON, for a report or a baseline."""
        return {
            "mrn": self.mrn,
            "stratum": self.stratum,
            "age": self.age,
            "problems": self.problems,
            "medications": self.medications,
            "flags": self.flags,
            "topics": self.topics,
            "deferred": self.deferred,
            "rubric": self.rubric,
        }


@dataclass
class CheckResult:
    """What the deterministic checks found for one case."""

    mrn: str
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def load_cohort(name: str = DEFAULT_COHORT, root: pathlib.Path | None = None) -> Cohort:
    """Read a cohort spec off disk."""
    path = (root or HERE) / ("cohort.json" if name == DEFAULT_COHORT else f"{name}.json")
    if not path.is_file():
        raise EvalError(f"no cohort spec at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "version", "seed", "patients", "years_of_history", "strata"):
        if key not in raw:
            raise EvalError(f"{path.name} is missing {key!r}")
    strata = tuple(
        Stratum(
            name=str(block["name"]),
            min_topics=int(block["min_topics"]),
            max_topics=int(block["max_topics"]),
            take=int(block["take"]),
        )
        for block in raw["strata"]
    )
    if not strata:
        raise EvalError(f"{path.name} defines no strata")
    return Cohort(
        name=str(raw["name"]),
        version=int(raw["version"]),
        seed=int(raw["seed"]),
        patients=int(raw["patients"]),
        years_of_history=int(raw["years_of_history"]),
        strata=strata,
    )


def build_cohort(session, cohort: Cohort) -> int:
    """Generate the cohort's patients. Returns how many were written."""
    from hdh.core.generators import build_dataset

    build_dataset(
        session,
        n_patients=cohort.patients,
        years_of_history=cohort.years_of_history,
        verbose=False,
        seed=cohort.seed,
    )
    session.commit()
    return cohort.patients


def _shape(session, patient) -> tuple:
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.rubric import select_rubric
    from hdh.modules.careplan.stratify import stratify
    from hdh.modules.careplan.triage import triage

    context = build_context(session, patient)
    if not context.problems:
        return ()
    flags = stratify(context)
    selected, deferred = triage(context, flags)
    return (context, flags, selected, deferred, select_rubric(context))


def select_cases(session, cohort: Cohort) -> list[Case]:
    """The cases, chosen by rule.

    Sorted by MRN inside each stratum and taken from the front. Not random,
    and deliberately not: a random draw would make two runs of the harness
    incomparable, which is the property the whole thing exists to provide.
    Unbiased selection matters *within* a stratum, and MRN order is
    unrelated to anything clinical.
    """
    from hdh.core.models import Patient

    by_stratum: dict[str, list[Case]] = {stratum.name: [] for stratum in cohort.strata}
    for patient in session.query(Patient).order_by(Patient.mrn).all():
        shape = _shape(session, patient)
        if not shape:
            continue
        context, flags, selected, deferred, rubric = shape
        stratum = next((s for s in cohort.strata if s.holds(len(selected))), None)
        if stratum is None:
            continue
        by_stratum[stratum.name].append(
            Case(
                mrn=context.mrn,
                stratum=stratum.name,
                age=context.age,
                problems=len(context.problems),
                medications=len(context.medications),
                flags=len(flags),
                topics=len(selected),
                deferred=len(deferred),
                rubric=rubric.rubric_id,
            )
        )

    cases: list[Case] = []
    for stratum in cohort.strata:
        available = by_stratum[stratum.name]
        cases.extend(available[: stratum.take])
    return cases


# ── deterministic checks: these are assertions, and they fail loudly ─────


def check_case(session, mrn: str) -> CheckResult:
    """What must hold for this chart, whatever a model says about it.

    None of this involves generation. These are properties of triage and
    the corpus, and each one is here because it broke at least once:

    - a problem in neither the selected topics nor the deferred list has
      vanished, which is what an aggregate flag did to hypothyroidism
    - an uncontrolled problem deferred while a controlled one is planned
      for inverts the priority the whole node exists to express
    - a topic that retrieves nothing cannot become a concern, so the chart
      silently loses it downstream — the shape of the four-chunk corpus
      failure, which no test caught because nothing checked coverage
    """
    from hdh.core.models import Patient
    from hdh.modules.careplan.generate import CONCERN_CORPORA, _candidates

    result = CheckResult(mrn=mrn)
    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        result.failures.append("no such patient")
        return result

    shape = _shape(session, patient)
    if not shape:
        result.failures.append("chart has no chronic problems")
        return result
    context, _flags, selected, deferred, _rubric = shape

    accounted = {topic.code for topic in selected + deferred if topic.code}
    for problem in context.problems:
        if problem.icd10 and problem.icd10 not in accounted:
            result.failures.append(
                f"{problem.description} ({problem.icd10}) is neither planned for nor deferred"
            )

    deferred_uncontrolled = [topic.label for topic in deferred if topic.basis == "recorded as not controlled"]
    if deferred_uncontrolled and any(not topic.is_flag for topic in selected):
        result.failures.append(
            "uncontrolled problem(s) deferred while controlled ones are planned for: "
            + ", ".join(deferred_uncontrolled)
        )

    from hdh.modules.careplan.retriever import build_store

    store = build_store(session)
    for topic in selected:
        if not _candidates(store, topic.query, CONCERN_CORPORA):
            result.failures.append(f"topic {topic.label!r} retrieves nothing — it cannot be cited")
    return result


# ── scores: tracked, never asserted ──────────────────────────────────────


@dataclass
class Measurement:
    """One case's scores across however many repeats it was run."""

    mrn: str
    stratum: str
    rubric: str
    runs: list[dict] = field(default_factory=list)

    def means(self) -> list[float]:
        return [run["mean"] for run in self.runs if run.get("mean") is not None]

    @property
    def mean(self) -> float | None:
        values = self.means()
        return round(statistics.mean(values), 3) if values else None

    @property
    def spread(self) -> float:
        """Observed range across repeats. A diagnostic, not the judgement.

        Kept because it is the number a reader intuits — "these runs landed
        between here and here" — but never compared against, for the reason
        in :attr:`deviation`.
        """
        values = self.means()
        return round(max(values) - min(values), 3) if len(values) > 1 else 0.0

    @property
    def deviation(self) -> float:
        """Standard deviation across repeats — what a delta is judged against.

        The first version judged on the range, and that was backwards. Range
        grows with sample size: the same four cases gave 0.50 at two repeats
        and 0.67 at three, from the same process. Judging against it meant
        **more measurement made a real change harder to detect**, which is
        the opposite of what more measurement is for. Standard deviation is
        stable across n.
        """
        values = self.means()
        return round(statistics.stdev(values), 3) if len(values) > 1 else 0.0


@dataclass
class Report:
    """Every case, and what the run as a whole says."""

    cohort: str
    measurements: list[Measurement] = field(default_factory=list)

    @property
    def mean(self) -> float | None:
        values = [m.mean for m in self.measurements if m.mean is not None]
        return round(statistics.mean(values), 3) if values else None

    @property
    def noise(self) -> float:
        """The pooled per-case standard deviation — what a delta must beat.

        Pooled as a root-mean-square rather than taking the worst case: one
        unusually variable chart should widen the band, not define it, and
        the cohort mean this is compared against is an average over all of
        them.
        """
        # Filtered on "was it measured", not on "was it non-zero". A case
        # whose three runs agreed exactly has an observed deviation of zero
        # and that is real information; a case run once also shows zero, and
        # that is an absence. Treating them alike would either inflate the
        # estimate or invent stability nobody measured.
        deviations = [m.deviation for m in self.measurements if len(m.means()) > 1]
        if not deviations:
            return 0.0
        return round((sum(d * d for d in deviations) / len(deviations)) ** 0.5, 3)

    @property
    def widest(self) -> float:
        """The largest observed range, for reporting alongside the noise."""
        spreads = [m.spread for m in self.measurements]
        return round(max(spreads), 3) if spreads else 0.0

    def as_dict(self) -> dict:
        """The whole run as JSON — this is the baseline format."""
        return {
            "cohort": self.cohort,
            "mean": self.mean,
            "noise": self.noise,
            "widest": self.widest,
            "cases": [
                {
                    "mrn": m.mrn,
                    "stratum": m.stratum,
                    "rubric": m.rubric,
                    "mean": m.mean,
                    "spread": m.spread,
                    "deviation": m.deviation,
                    "runs": m.runs,
                }
                for m in self.measurements
            ],
        }


def compare(report: Report, baseline: dict) -> list[str]:
    """What changed since the baseline, and whether it is distinguishable.

    The last line is the point of the whole module. A delta smaller than the
    noise the run itself observed is not an improvement or a regression; it
    is the same number twice, and saying so is more useful than a arrow
    pointing the way somebody hoped.
    """
    lines = []
    before, after = baseline.get("mean"), report.mean
    if before is None or after is None:
        return ["no comparable baseline mean"]

    by_mrn = {case["mrn"]: case for case in baseline.get("cases", [])}
    for measurement in report.measurements:
        previous = by_mrn.get(measurement.mrn, {}).get("mean")
        if previous is None or measurement.mean is None:
            lines.append(f"  {measurement.mrn:<14} {measurement.mean}  (no baseline)")
            continue
        delta = round(measurement.mean - previous, 3)
        lines.append(f"  {measurement.mrn:<14} {previous} -> {measurement.mean}  ({delta:+.2f})")

    delta = round(after - before, 3)
    noise = max(report.noise, baseline_noise(baseline))
    lines.append("")
    lines.append(f"  overall {before} -> {after}  ({delta:+.2f})")
    if noise and abs(delta) <= noise:
        lines.append(
            f"  NOT DISTINGUISHABLE — the change is {abs(delta):.2f} and the observed "
            f"run-to-run spread is {noise:.2f}. More repeats, or a bigger change."
        )
    elif noise:
        lines.append(f"  outside the observed spread of {noise:.2f}")
    else:
        lines.append("  no repeats were run, so there is no noise floor to compare against")
    return lines


def baseline_noise(baseline: Mapping) -> float:
    """The baseline's noise, recomputed rather than trusted.

    A baseline written before the statistic changed carries a *range* in its
    ``noise`` field, which is a different and larger number than the pooled
    standard deviation used now. Recomputing from the per-run means it also
    stored keeps old baselines comparable instead of quietly holding new
    runs to a stale, wider bar.
    """
    deviations = []
    for case in baseline.get("cases", []):
        means = [run["mean"] for run in case.get("runs", []) if run.get("mean") is not None]
        if len(means) > 1:
            deviations.append(statistics.stdev(means))  # zero counts; it was measured
    if deviations:
        return round((sum(d * d for d in deviations) / len(deviations)) ** 0.5, 3)
    return float(baseline.get("noise") or 0.0)


def load_baseline(path: pathlib.Path) -> dict:
    if not path.is_file():
        raise EvalError(f"no baseline at {path} — run with --save to write one")
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(path: pathlib.Path, report: Report) -> None:
    path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")


def measure_case(session, case: Case, services, *, repeat: int = 1, revise: bool = False) -> Measurement:
    """Generate and grade one case ``repeat`` times.

    Each repeat is a fresh plan. Nothing is asserted about the scores here —
    they are returned for the report to record.
    """
    from hdh.core.models import Patient
    from hdh.modules.careplan.plan import generate_plan

    patient = session.query(Patient).filter(Patient.mrn == case.mrn).one()
    measurement = Measurement(mrn=case.mrn, stratum=case.stratum, rubric=case.rubric)
    for _attempt in range(repeat):
        result = generate_plan(session, patient, services=services, revise=revise)
        evaluation, rubric = result.evaluation, result.rubric
        if evaluation is None or rubric is None:
            measurement.runs.append({"mean": None, "error": "nothing graded"})
            continue
        measurement.runs.append(
            {
                "mean": evaluation.overall,
                "verdict": evaluation.verdict(rubric),
                "interventions": len(result.draft.interventions),
                "scores": {score.dimension_id: score.score for score in evaluation.scores},
            }
        )
    return measurement


@dataclass(frozen=True)
class RunSettings:
    """How to run the cohort, as distinct from what to run it on."""

    repeat: int = 1
    revise: bool = False
    cohort: str = DEFAULT_COHORT

    @property
    def measures_noise(self) -> bool:
        """One run per case measures no noise floor, and cannot pretend to."""
        return self.repeat > 1


def run(session, cases: Sequence[Case], services, settings: RunSettings, on_case=None) -> Report:
    """Measure every case. Returns the report; writes no baseline."""
    report = Report(cohort=settings.cohort)
    for case in cases:
        measurement = measure_case(session, case, services, repeat=settings.repeat, revise=settings.revise)
        report.measurements.append(measurement)
        if on_case is not None:
            on_case(measurement)
    return report
