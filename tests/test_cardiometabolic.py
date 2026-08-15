"""Milestone B of clinical-breadth (issue #28): the webs are real.

Comorbidity links multiply annual onset, severity stages evolve on a
configurable cadence, rolled onsets carry clinically ordered dates, and
a seeded run is fully reproducible. The population tests run on a FIXED
seed, so every assertion is deterministic — ranges express clinical
plausibility (design §10 Q2), not epidemiology."""

import random

import pytest

from hdh.core.cardiometabolic import CardiometabolicPack
from hdh.core.conditions import SamplingContext, default_catalog
from hdh.core.generators import build_dataset
from hdh.core.models import Condition, Patient, Prescription, Visit, get_engine, get_session

SEED = 20260814


def _ctx(**overrides) -> SamplingContext:
    from hdh.core.models import Sex

    defaults = dict(age=70, sex=Sex.MALE, month=1, rng=random.Random(5))
    defaults.update(overrides)
    return SamplingContext(**defaults)


# ── unit: the webs and the stages ────────────────────────────────────────────


def test_comorbidity_links_multiply_onset_rate():
    catalog = default_catalog()
    rolls = 20_000
    with_links = sum(
        any(
            p.name == "ckd"
            for p in catalog.annual_onsets(
                _ctx(established=frozenset({"hypertension", "type2_diabetes"}), rng=random.Random(i))
            )
        )
        for i in range(rolls)
    )
    without = sum(
        any(p.name == "ckd" for p in catalog.annual_onsets(_ctx(rng=random.Random(i)))) for i in range(rolls)
    )
    # HTN x3 * T2DM x3 = 9x the base rate — allow generous sampling slack
    assert with_links > without * 4, (with_links, without)


def test_onset_needs_minimum_age_and_absence():
    catalog = default_catalog()
    assert not any(
        p.name == "ckd"
        for i in range(5_000)
        for p in catalog.annual_onsets(
            _ctx(age=40, established=frozenset({"hypertension"}), rng=random.Random(i))
        )
    )
    assert not any(
        p.name == "ckd"
        for i in range(5_000)
        for p in catalog.annual_onsets(
            _ctx(established=frozenset({"ckd", "hypertension"}), rng=random.Random(i))
        )
    )


def test_stage_profile_steps_and_cadence_equivalence():
    staging = default_catalog().get("ckd").staging
    assert [s.icd10_code for s in staging.stages] == ["N18.31", "N18.32", "N18.4", "N18.5"]

    def final_stage(periods_per_year: int, seed: int) -> int:
        rng = random.Random(seed)
        index = 0
        for _ in range(4 * periods_per_year):  # four simulated years
            index = staging.step(index, rng, periods_per_year)
        return index

    yearly = [final_stage(1, s) for s in range(3_000)]
    quarterly = [final_stage(4, s) for s in range(3_000)]
    assert 0 < sum(yearly) / len(yearly) < 2.0  # trajectories move, but not to the ceiling
    # cadence changes granularity, not the long-run trajectory
    assert abs(sum(yearly) / len(yearly) - sum(quarterly) / len(quarterly)) < 0.25


def test_pack_authoring_is_complete():
    for profile in CardiometabolicPack().conditions():
        assert profile.chronic and profile.snomed_code and profile.onset is not None
        assert profile.rx_options, profile.name


# ── population: one seeded build, deterministic assertions ───────────────────


@pytest.fixture(scope="module")
def population(tmp_path_factory):
    db = tmp_path_factory.mktemp("breadth") / "population.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    build_dataset(session, n_patients=250, years_of_history=4, verbose=False, seed=SEED)
    yield session
    session.close()
    engine.dispose()


def _patients_with(session, code_prefix: str) -> dict[int, Condition]:
    rows = (
        session.query(Condition)
        .filter(Condition.chronic.is_(True), Condition.icd10_code.like(f"{code_prefix}%"))
        .all()
    )
    return {row.patient_id: row for row in rows}


def test_cardiovascular_cohort_is_no_longer_a_monoculture(population):
    """The agent's original finding, closed: I-chapter chronic disease now
    spans multiple distinct conditions, not just I10."""
    codes = {
        row.icd10_code
        for row in population.query(Condition)
        .filter(Condition.chronic.is_(True), Condition.icd10_code.like("I%"))
        .all()
    }
    assert "I10" in codes
    assert len(codes) >= 3, codes  # CAD / HF / AFib joined hypertension


def test_new_pack_conditions_materialize(population):
    for prefix in ("N18", "I25.10", "J45"):
        assert _patients_with(population, prefix), f"no chronic rows for {prefix}"


def test_ckd_arrives_through_the_webs(population):
    """Most CKD patients must carry an antecedent driver (HTN or T2DM)."""
    ckd = _patients_with(population, "N18")
    htn = _patients_with(population, "I10")
    t2dm = _patients_with(population, "E11")
    with_driver = [pid for pid in ckd if pid in htn or pid in t2dm]
    assert len(with_driver) / len(ckd) >= 0.7, (len(with_driver), len(ckd))


def test_rolled_onsets_are_clinically_ordered(population):
    """CKD onset dates postdate the antecedent's onset in the vast majority
    of charts — the trajectory reads correctly."""
    ckd = _patients_with(population, "N18")
    htn = _patients_with(population, "I10")
    ordered = total = 0
    for pid, ckd_row in ckd.items():
        driver = htn.get(pid)
        if driver is None:
            continue
        total += 1
        if ckd_row.onset_date >= driver.onset_date:
            ordered += 1
    assert total > 0 and ordered / total >= 0.8, (ordered, total)


def test_afib_patients_are_anticoagulated(population):
    afib_ids = set(_patients_with(population, "I48"))
    anticoagulated = {
        visit.patient_id
        for visit, _rx in (
            population.query(Visit, Prescription)
            .join(Prescription, Prescription.visit_id == Visit.id)
            .filter(Prescription.drug_class.like("Anticoagulant%"))
            .all()
        )
    }
    covered = afib_ids & anticoagulated
    assert len(covered) / len(afib_ids) >= 0.8, (len(covered), len(afib_ids))


def test_ckd_stages_show_a_trajectory(population):
    """Four years must move SOMEONE off stage 3a (the user's Q1 point)."""
    stage_codes = {row.icd10_code for row in _patients_with(population, "N18").values()}
    assert len(stage_codes) >= 2, stage_codes


def test_same_seed_reproduces_the_dataset(tmp_path):
    def snapshot(path):
        engine = get_engine(str(path))
        session = get_session(engine)
        build_dataset(session, n_patients=30, years_of_history=2, verbose=False, seed=99)
        data = {
            "mrns": sorted(mrn for (mrn,) in session.query(Patient.mrn)),
            "conditions": sorted(
                (c.patient_id, c.icd10_code, str(c.onset_date)) for c in session.query(Condition)
            ),
            "visits": session.query(Visit).count(),
        }
        session.close()
        engine.dispose()
        return data

    assert snapshot(tmp_path / "a.db") == snapshot(tmp_path / "b.db")
