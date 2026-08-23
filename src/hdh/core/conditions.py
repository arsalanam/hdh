"""Condition contracts and the catalog service (design clinical-breadth.md).

The strong-typed, immutable core of the disease engine: frozen profile
contracts (§2), the encapsulated :class:`ConditionCatalog` (§3), and the
:class:`ConditionSource` pack protocol (§4). Content lives in packs
(``disease_engine`` ships ``family-medicine-core``); sampling is a pure
function of :class:`SamplingContext` — the RNG is injected, nothing
reads global state, and no consumer ever sees a mutable structure.
"""

from __future__ import annotations

import enum
import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from hdh.core.models import Sex, VisitType

_log = logging.getLogger("hdh.conditions")


class CatalogError(Exception):
    """A catalog assembly violation (duplicate names, empty packs…)."""


class AgeBand(enum.Enum):
    """The condition-catalog age bands (no magic strings)."""

    INFANT = "infant"  # 0–2
    CHILD = "child"  # 3–12
    TEEN = "teen"  # 13–17
    YOUNG_ADULT = "young_adult"  # 18–35
    ADULT = "adult"  # 36–50
    MIDDLE_AGED = "middle_aged"  # 51–65
    SENIOR = "senior"  # 65+

    @staticmethod
    def for_age(age: int) -> AgeBand:
        """Map an age in years to its band."""
        if age <= 2:
            return AgeBand.INFANT
        if age <= 12:
            return AgeBand.CHILD
        if age <= 17:
            return AgeBand.TEEN
        if age <= 35:
            return AgeBand.YOUNG_ADULT
        if age <= 50:
            return AgeBand.ADULT
        if age <= 65:
            return AgeBand.MIDDLE_AGED
        return AgeBand.SENIOR


class RiskKind(enum.Enum):
    """The onset risk-factor kinds a profile may declare."""

    FAMILY_HISTORY = "family_history"
    SMOKER = "smoker"
    BMI_OVER = "bmi_over"


@dataclass(frozen=True)
class RiskFactor:
    """One onset risk factor; ``threshold`` applies to BMI_OVER only.

    Without ``multiplier`` the factor is a baseline-seeding guarantee
    (the legacy force semantics); with one, it scales the annual onset
    rate instead (e.g. smoking ×2 for coronary artery disease)."""

    kind: RiskKind
    threshold: float | None = None
    multiplier: float | None = None


@dataclass(frozen=True)
class ComorbidityLink:
    """Established ``condition`` multiplies this one's annual onset rate
    by ``relative_risk`` (milestone B — the webs)."""

    condition: str
    relative_risk: float


@dataclass(frozen=True)
class OnsetProfile:
    """When and why a chronic condition begins.

    Milestone A uses the baseline fields (chart-start seeding, faithful
    to the legacy rules): at ``min_age``+, the condition seeds with
    ``baseline_probability``, and any matching ``force_factors`` seed it
    outright. ``annual_rate``/``comorbid_links`` power milestone B's
    yearly onset rolls and stay inert until then."""

    min_age: int
    baseline_probability: float
    force_factors: tuple[RiskFactor, ...] = ()
    hereditary_key: str | None = None  # family-history key that forces onset
    annual_rate: float = 0.0
    female_multiplier: float = 1.0  # sex factor on the annual rate
    comorbid_links: tuple[ComorbidityLink, ...] = ()


@dataclass(frozen=True)
class Stage:
    """One severity stage of a staged chronic condition — its own codes."""

    icd10_code: str
    description: str
    snomed_code: str | None = None


@dataclass(frozen=True)
class StageProfile:
    """Severity trajectory for a chronic condition (design §5 amendment).

    Ordered mild→severe; probabilities are ANNUAL and are rescaled to the
    run's evaluation cadence (yearly or quarterly) as 1-(1-p)^(1/periods),
    so cadence changes granularity, never the long-run trajectory."""

    stages: tuple[Stage, ...]
    progress_probability: float  # annual chance of moving one stage worse
    improve_probability: float = 0.0  # annual chance of moving one stage better
    start_index: int = 0

    def step(self, index: int, rng: random.Random, periods_per_year: int) -> int:
        """One evaluation-period roll from ``index``; clamped to the range."""
        progress = 1 - (1 - self.progress_probability) ** (1 / periods_per_year)
        improve = 1 - (1 - self.improve_probability) ** (1 / periods_per_year)
        roll = rng.random()
        if roll < progress:
            return min(index + 1, len(self.stages) - 1)
        if roll < progress + improve:
            return max(index - 1, 0)
        return index


@dataclass(frozen=True)
class LabSpec:
    """One lab test: LOINC code, reference range, and the condition's shift."""

    test_name: str
    loinc_code: str
    unit: str
    ref_low: float
    ref_high: float
    normal_mean: float
    normal_sd: float
    condition_shift: float = 0.0
    condition_shift_sd: float = 0.0


class RxKind(enum.Enum):
    """What a formulary entry actually IS.

    ``rx_options`` carried three different things under one name, and the
    chart could not tell them apart: real drugs, referrals, and plain
    advice. Charting the last two as prescriptions made the record claim a
    patient had been PRESCRIBED "Rest & fluids", and gave every referral an
    em-dash dose and frequency (issue #49; design service-requests §5).
    """

    DRUG = "drug"  # → Prescription
    REFERRAL = "referral"  # → a REFERRAL ServiceRequest
    ADVICE = "advice"  # → the note says it; the chart records nothing


@dataclass(frozen=True)
class RxSpec:
    """One entry in a condition's formulary — a drug unless it says so."""

    drug_name: str
    drug_class: str
    dose: str
    frequency: str
    duration_days: int | None  # None = chronic / ongoing
    refills: int = 0
    kind: RxKind = RxKind.DRUG
    #: The drug this entry IS, when a licensed RxNorm release was available
    #: to check it against. Optional on purpose: the catalog must stay
    #: authorable without one, and an unverified guess would be worse than
    #: the free text it replaces (design rxnorm §6, §11 Q4).
    rxcui: str | None = None


@dataclass(frozen=True)
class ConditionProfile:
    """The complete contract for one condition: identity and codes, how a
    visit for it looks (vitals deltas, labs, formulary, follow-up),
    when it occurs (band weights, seasonality, sex limit), and — for
    chronic conditions — how it begins (:class:`OnsetProfile`).

    Deltas are ``(mean, sd)`` pairs consumed by the generator's gaussian
    draws; every collection is a tuple. The instance is deeply frozen."""

    name: str
    icd10_code: str
    description: str
    chief_complaint: str
    visit_type: VisitType
    chronic: bool = False
    snomed_code: str | None = None
    sex_limit: Sex | None = None
    bp_sys_delta: tuple[float, float] = (0, 5)
    bp_dia_delta: tuple[float, float] = (0, 3)
    hr_delta: tuple[float, float] = (0, 5)
    rr_delta: tuple[float, float] = (0, 2)
    temp_delta: tuple[float, float] = (0.0, 0.2)
    spo2_delta: tuple[float, float] = (0, 1)
    pain: tuple[float, float] = (0, 1)
    labs: tuple[LabSpec, ...] = ()
    rx_options: tuple[RxSpec, ...] = ()
    rx_pick_all: bool = False
    follow_up_days: int | None = None
    # month index 1–12 → multiplier; entry absent = 1.0
    seasonal_weights: tuple[tuple[int, float], ...] = ()
    # band → visit-sampling weight; absent band = not offered there
    visit_weights: tuple[tuple[AgeBand, float], ...] = ()
    onset: OnsetProfile | None = None
    staging: StageProfile | None = None  # severity trajectory (chronic only)

    def seasonal_multiplier(self, month: int) -> float:
        """The seasonal weight for a month (1.0 when unspecified)."""
        for m, weight in self.seasonal_weights:
            if m == month:
                return weight
        return 1.0


@dataclass(frozen=True)
class SamplingContext:
    """Every input sampling needs, made explicit — nothing global.

    ``family_history`` holds hereditary keys (e.g. "diabetes") present in
    the patient's family; ``rng`` is the injected random source, so the
    same seed reproduces the same chart (and per-household seeds stay
    possible for the deferred parallel-generation plan)."""

    age: int
    sex: Sex
    month: int
    established: frozenset[str] = frozenset()
    family_history: frozenset[str] = frozenset()
    smoker: bool = False
    bmi: float = 25.0
    rng: random.Random = field(default_factory=random.Random)


class ConditionSource(Protocol):
    """One pack of authored conditions (a specialty set, a locale set,
    a research scenario…) — the pluggable unit (§4)."""

    name: str

    def conditions(self) -> tuple[ConditionProfile, ...]:
        """Return every profile this pack contributes."""
        ...


_ESTABLISHED_FOLLOW_UP_BOOST = 1.8  # chronic conditions revisit more often


class ConditionCatalog:
    """Sampling + lookup over an immutable set of ConditionProfiles.

    The only public surface of the disease engine (§3): internals are
    private, every return value is a tuple or a frozen profile, and all
    randomness comes from the caller's :class:`SamplingContext`."""

    def __init__(self, profiles: Iterable[ConditionProfile]) -> None:
        self._pack_names: dict[str, str] = {}  # name -> authoring pack (build_catalog fills)
        self._profiles: dict[str, ConditionProfile] = {}
        for profile in profiles:
            if profile.name in self._profiles:
                raise CatalogError(
                    f"duplicate condition '{profile.name}' — clinical content must not "
                    "silently override; rename or remove one of the packs' definitions"
                )
            self._profiles[profile.name] = profile
        if not self._profiles:
            raise CatalogError("catalog built with no conditions")
        self._band_pools: dict[AgeBand, tuple[tuple[ConditionProfile, float], ...]] = {
            band: tuple(
                (profile, weight)
                for profile in self._profiles.values()
                for b, weight in profile.visit_weights
                if b is band
            )
            for band in AgeBand
        }

    # ── lookup ───────────────────────────────────────────────────────────

    def get(self, name: str) -> ConditionProfile:
        """The profile for a name; unknown names fail loudly."""
        try:
            return self._profiles[name]
        except KeyError:
            raise KeyError(f"unknown condition '{name}' (known: {sorted(self._profiles)})") from None

    def names(self) -> tuple[str, ...]:
        """Every condition name, sorted."""
        return tuple(sorted(self._profiles))

    def chronic(self) -> tuple[ConditionProfile, ...]:
        """Every chronic profile, in name order."""
        return tuple(p for _n, p in sorted(self._profiles.items()) if p.chronic)

    def pack_of(self, name: str) -> str:
        """The pack that authored a condition ("?" for direct assembly)."""
        self.get(name)  # loud on unknown names
        return self._pack_names.get(name, "?")

    # ── sampling ─────────────────────────────────────────────────────────

    def sample_visit_condition(self, ctx: SamplingContext) -> ConditionProfile:
        """One condition for a visit, weighted by age band, season, and
        established-chronic follow-up boost; sex-limited profiles are
        excluded outright."""
        pool = [
            (profile, weight)
            for profile, weight in self._band_pools[AgeBand.for_age(ctx.age)]
            if profile.sex_limit is None or profile.sex_limit == ctx.sex
        ]
        if not pool:
            raise CatalogError(f"no conditions offered for band {AgeBand.for_age(ctx.age).value}")
        weights = [
            base
            * profile.seasonal_multiplier(ctx.month)
            * (_ESTABLISHED_FOLLOW_UP_BOOST if profile.name in ctx.established else 1.0)
            for profile, base in pool
        ]
        return ctx.rng.choices([profile for profile, _w in pool], weights=weights, k=1)[0]

    def seed_chronic(self, ctx: SamplingContext) -> tuple[ConditionProfile, ...]:
        """Chart-start chronic seeding from each profile's OnsetProfile:
        age-eligible conditions seed at their baseline probability, and
        matching force factors (family history, smoking, BMI) seed them
        outright — the legacy rules, now declarative."""
        seeded = []
        for profile in self.chronic():
            onset = profile.onset
            if onset is None or ctx.age < onset.min_age:
                continue
            if self._forced(onset, ctx) or ctx.rng.random() < onset.baseline_probability:
                seeded.append(profile)
        return tuple(seeded)

    def annual_onsets(self, ctx: SamplingContext) -> tuple[ConditionProfile, ...]:
        """One year's onset rolls for not-yet-established chronic
        conditions — where the comorbidity webs act (design §5): each
        established link multiplies the annual rate by its relative
        risk, so CKD arrives *because of* the hypertension years."""
        onsets = []
        for profile in self.chronic():
            onset = profile.onset
            if (
                onset is None
                or onset.annual_rate <= 0
                or ctx.age < onset.min_age
                or profile.name in ctx.established
            ):
                continue
            rate = onset.annual_rate
            if ctx.sex == Sex.FEMALE:
                rate *= onset.female_multiplier
            for link in onset.comorbid_links:
                if link.condition in ctx.established:
                    rate *= link.relative_risk
            for factor in onset.force_factors:
                if factor.multiplier is None:
                    continue
                if factor.kind is RiskKind.SMOKER and ctx.smoker:
                    rate *= factor.multiplier
                elif (
                    factor.kind is RiskKind.BMI_OVER
                    and factor.threshold is not None
                    and ctx.bmi > factor.threshold
                ):
                    rate *= factor.multiplier
            if ctx.rng.random() < min(rate, 0.95):
                onsets.append(profile)
        return tuple(onsets)

    @staticmethod
    def _forced(onset: OnsetProfile, ctx: SamplingContext) -> bool:
        if onset.hereditary_key and onset.hereditary_key in ctx.family_history:
            return True
        for factor in onset.force_factors:
            if factor.multiplier is not None:
                continue  # a rate factor (annual rolls), not a guarantee
            if factor.kind is RiskKind.SMOKER and ctx.smoker:
                return True
            if (
                factor.kind is RiskKind.BMI_OVER
                and factor.threshold is not None
                and ctx.bmi > factor.threshold
            ):
                return True
        return False


def build_catalog(sources: Iterable[ConditionSource]) -> ConditionCatalog:
    """Merge packs into one catalog (duplicate names are a hard error);
    each profile remembers which pack authored it (``pack_of``)."""
    profiles: list[ConditionProfile] = []
    pack_names: dict[str, str] = {}
    for source in sources:
        for profile in source.conditions():
            profiles.append(profile)
            pack_names.setdefault(profile.name, source.name)
    catalog = ConditionCatalog(profiles)
    catalog._pack_names = pack_names  # noqa: SLF001 — assembler wiring its own product
    return catalog


def module_packs(strict: bool = False) -> list[ConditionSource]:
    """Packs contributed by feature modules (``GENERATOR_MODULES``).

    The FHIR_MODULES pattern: runtime is fail-soft (an absent optional
    module never breaks generation); tests load with ``strict=True`` so
    real defects fail loud."""
    from importlib import import_module

    from hdh.modules import GENERATOR_MODULES

    packs: list[ConditionSource] = []
    for module_path in GENERATOR_MODULES:
        try:
            packs.extend(import_module(module_path).condition_packs())
        except Exception:
            if strict:
                raise
            _log.warning("condition-pack module %s failed to load — skipped", module_path)
    return packs


def default_catalog() -> ConditionCatalog:
    """The standard assembly: core packs + discovered module packs."""
    from hdh.core.cardiometabolic import CardiometabolicPack
    from hdh.core.disease_engine import FamilyMedicineCorePack

    return build_catalog([FamilyMedicineCorePack(), CardiometabolicPack(), *module_packs()])
