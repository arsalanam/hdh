"""Milestone A of clinical-breadth (issue #28): the contracts hold.

Frozen profiles, the encapsulated catalog, injected RNG determinism,
faithful chronic seeding, and the pack merge rules — all before any new
clinical content lands (milestone B builds on these guarantees)."""

import dataclasses
import random

import pytest

from hdh.core.conditions import (
    AgeBand,
    CatalogError,
    ConditionProfile,
    OnsetProfile,
    SamplingContext,
    build_catalog,
    default_catalog,
)
from hdh.core.disease_engine import FamilyMedicineCorePack
from hdh.core.models import Sex, VisitType

LEGACY_CHRONIC = {
    "hypertension",
    "type2_diabetes",
    "hyperlipidemia",
    "copd",
    "osteoarthritis",
    "hypothyroidism",
    "obesity",
}


def _ctx(**overrides) -> SamplingContext:
    defaults = dict(age=50, sex=Sex.FEMALE, month=1, rng=random.Random(7))
    defaults.update(overrides)
    return SamplingContext(**defaults)


def test_profiles_are_deeply_immutable():
    profile = default_catalog().get("hypertension")
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.icd10_code = "X99"  # type: ignore[misc]
    assert isinstance(profile.labs, tuple) and isinstance(profile.rx_options, tuple)
    assert isinstance(profile.visit_weights, tuple)


def test_core_pack_preserves_the_legacy_catalog():
    core_only = build_catalog([FamilyMedicineCorePack()])
    assert {p.name for p in core_only.chronic()} == LEGACY_CHRONIC  # the pack, unchanged
    catalog = default_catalog()
    assert len(catalog.names()) >= 30
    assert LEGACY_CHRONIC <= {p.name for p in catalog.chronic()}  # milestone B adds, never removes
    htn = catalog.get("hypertension")
    assert htn.icd10_code == "I10" and htn.visit_type is VisitType.FOLLOW_UP
    assert htn.snomed_code == "59621000"  # authored opportunistically (design Q3)
    bands = dict(htn.visit_weights)
    assert bands[AgeBand.MIDDLE_AGED] == 2.5  # the legacy AGE_WEIGHTS value


def test_get_unknown_condition_is_loud():
    with pytest.raises(KeyError, match="unknown condition"):
        default_catalog().get("dragon_pox")


def test_duplicate_condition_names_are_a_hard_error():
    class ClashPack:
        name = "clash"

        def conditions(self):
            return (
                ConditionProfile(
                    name="hypertension",
                    icd10_code="I10",
                    description="dup",
                    chief_complaint="dup",
                    visit_type=VisitType.ACUTE,
                ),
            )

    with pytest.raises(CatalogError, match="duplicate condition 'hypertension'"):
        build_catalog([FamilyMedicineCorePack(), ClashPack()])


def test_throwaway_pack_merges_cleanly():
    class NeuroPack:
        name = "neuro-demo"

        def conditions(self):
            return (
                ConditionProfile(
                    name="migraine",
                    icd10_code="G43.909",
                    description="Migraine, unspecified",
                    chief_complaint="Recurrent headache",
                    visit_type=VisitType.ACUTE,
                    visit_weights=((AgeBand.ADULT, 1.0),),
                ),
            )

    catalog = build_catalog([FamilyMedicineCorePack(), NeuroPack()])
    assert catalog.get("migraine").icd10_code == "G43.909"


def test_injected_rng_makes_sampling_deterministic():
    catalog = default_catalog()
    draws_a = [catalog.sample_visit_condition(_ctx(rng=random.Random(42))).name for _ in range(20)]
    draws_b = [catalog.sample_visit_condition(_ctx(rng=random.Random(42))).name for _ in range(20)]
    assert draws_a == draws_b


def test_seed_chronic_honors_legacy_guarantees():
    catalog = default_catalog()
    # family history of hypertension at 50+ ALWAYS seeds it (legacy rule)
    seeded = {p.name for p in catalog.seed_chronic(_ctx(age=50, family_history=frozenset({"hypertension"})))}
    assert "hypertension" in seeded
    # a 65+ smoker always gets COPD; BMI over 27 always gets diabetes
    seeded = {p.name for p in catalog.seed_chronic(_ctx(age=66, smoker=True, bmi=30.0))}
    assert {"copd", "type2_diabetes"} <= seeded
    # nobody under 45 is baseline-seeded at all
    assert catalog.seed_chronic(_ctx(age=30, smoker=True, bmi=35.0)) == ()


def test_sex_limited_conditions_never_sampled_for_males():
    catalog = default_catalog()
    rng = random.Random(2026)
    names = {
        catalog.sample_visit_condition(_ctx(age=28, sex=Sex.MALE, month=6, rng=rng)).name for _ in range(400)
    }
    assert "contraception_consult" not in names and "uti" not in names


def test_seasonality_still_shapes_sampling():
    catalog = default_catalog()
    rng = random.Random(11)
    january = sum(
        catalog.sample_visit_condition(_ctx(age=40, month=1, rng=rng)).name == "influenza" for _ in range(600)
    )
    july = sum(
        catalog.sample_visit_condition(_ctx(age=40, month=7, rng=rng)).name == "influenza" for _ in range(600)
    )
    assert january > july  # FLU_SEASON weights survived the restructure


def test_onset_profile_is_declarative_data():
    onset = default_catalog().get("type2_diabetes").onset
    assert isinstance(onset, OnsetProfile)
    assert onset.min_age == 45 and onset.hereditary_key == "diabetes"


def test_build_dataset_accepts_an_injected_catalog(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Patient, get_engine, get_session

    engine = get_engine(str(tmp_path / "tiny.db"))
    session = get_session(engine)
    build_dataset(session, n_patients=3, years_of_history=1, verbose=False, catalog=default_catalog())
    assert session.query(Patient).count() == 3
    session.close()
    engine.dispose()


def test_module_pack_discovery(monkeypatch):
    """GENERATOR_MODULES packs join the default assembly with zero core
    edits (milestone C) — and strict mode surfaces broken modules."""
    import sys
    import types

    from hdh.core.conditions import module_packs

    fake = types.ModuleType("fake_neuro_pack")

    class NeuroPack:
        name = "neuro-demo"

        def conditions(self):
            return (
                ConditionProfile(
                    name="migraine",
                    icd10_code="G43.909",
                    description="Migraine, unspecified",
                    chief_complaint="Recurrent headache",
                    visit_type=VisitType.ACUTE,
                    visit_weights=((AgeBand.ADULT, 1.0),),
                ),
            )

    fake.condition_packs = lambda: [NeuroPack()]
    monkeypatch.setitem(sys.modules, "fake_neuro_pack", fake)
    monkeypatch.setattr("hdh.modules.GENERATOR_MODULES", ("fake_neuro_pack",))

    catalog = default_catalog()
    assert catalog.get("migraine").icd10_code == "G43.909"
    assert catalog.pack_of("migraine") == "neuro-demo"
    assert catalog.pack_of("hypertension") == "family-medicine-core"
    assert catalog.pack_of("ckd") == "cardiometabolic"

    monkeypatch.setattr("hdh.modules.GENERATOR_MODULES", ("no_such_module",))
    assert module_packs() == []  # fail-soft at runtime
    with pytest.raises(ModuleNotFoundError):
        module_packs(strict=True)  # fail-loud in tests
