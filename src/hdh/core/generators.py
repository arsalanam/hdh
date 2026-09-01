"""
Generators for synthetic Family Medicine patients and their visit histories.

The chart expansion (docs/design/core-chart-expansion.md §6) makes the
generator family-aware: patients are built as households, hereditary risk
derives from relatives' ACTUAL generated conditions (not random flags),
providers have continuity, every visit stores a SOAP note, and the chart
entities — allergies, medication statements, immunizations, procedures —
are populated with medically coherent data.
"""

import random
import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

from faker import Faker
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update

from .conditions import ConditionCatalog, LabSpec, RxKind, SamplingContext, Stage, default_catalog
from .models import (
    Allergy,
    AllergySeverity,
    Condition,
    ConditionStatus,
    FamilyHistory,
    FamilyMember,
    Immunization,
    LabResult,
    LabStatus,
    MedicationDispense,
    MedicationStatement,
    MedicationStatus,
    NoteType,
    Patient,
    Prescription,
    Procedure,
    Provider,
    RequestOrigin,
    RequestStatus,
    ServiceKind,
    ServiceRequest,
    Specialty,
    Visit,
    VisitNote,
    Vital,
)

fake = Faker("en_US")

#: The default generation date. Bound at import so one run cannot straddle
#: midnight and produce two half-charts on different footings.
TODAY = date.today()


@dataclass(frozen=True)
class RunScope:
    """Per-run generation dependencies, injected once by build_dataset:
    the condition catalog, the practice providers, the history depth,
    and the run RNG (deterministic seeding lands with milestone B)."""

    catalog: ConditionCatalog
    providers: tuple
    years: int
    rng: random.Random
    periods_per_year: int = 1  # staging cadence: 1=yearly, 4=quarterly
    #: The day the chart is generated *as of*. Every date in a chart is
    #: relative to this one — dates of birth, the start of the history
    #: window, immunisation seasons — so reading the wall clock instead
    #: makes "same seed, same dataset" true only within a single day.
    #:
    #: It is not a uniform shift, which is what makes it a real bug rather
    #: than an offset: a one-day move changed how many patients reached CKD
    #: from 40 to 29 in a 500-patient run, because the history window slid
    #: across staging boundaries. CI caught it by crossing midnight between
    #: two merges that changed no generation code.
    as_of: date = TODAY


Faker.seed(42)
random.seed(42)


# ─── Reference data ───────────────────────────────────────────────────────────

INSURERS = (
    "Blue Cross Blue Shield",
    "Aetna",
    "UnitedHealthcare",
    "Cigna",
    "Humana",
    "Medicare",
    "Medicaid",
    "Kaiser Permanente",
    "Anthem",
    "Molina Healthcare",
)

BLOOD_TYPES = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")

ALLERGY_SPECS = (
    ("Penicillin", "rash", "moderate"),
    ("Sulfa drugs", "hives", "moderate"),
    ("Aspirin", "GI upset", "mild"),
    ("Ibuprofen", "swelling", "mild"),
    ("Codeine", "nausea", "mild"),
    ("Latex", "contact dermatitis", "mild"),
    ("Shellfish", "anaphylaxis", "severe"),
    ("Peanuts", "anaphylaxis", "severe"),
    ("Tree nuts", "hives", "moderate"),
    ("Bee stings", "local swelling", "moderate"),
    ("Amoxicillin", "rash", "moderate"),
    ("Erythromycin", "GI upset", "mild"),
)

PRACTICE = (
    # (identifier, name, specialty code)
    ("NPI1000001", "Dr. Sarah Mitchell, MD", "FM"),
    ("NPI1000002", "Dr. James O'Brien, MD", "FM"),
    ("NPI1000003", "Dr. Priya Sharma, MD", "FM"),
    ("NPI1000004", "Dr. Robert Chen, MD", "IM"),
    ("NPI1000005", "Dr. Angela Torres, DO", "FM"),
    ("NPI1000006", "Dr. Michael Park, MD", "PED"),
    ("NPI1000007", "Jordan Reyes, NP", "FM"),
    ("NPI1000008", "Casey Lin, PA-C", "FM"),
)

SPECIALTIES = (("FM", "Family Medicine"), ("IM", "Internal Medicine"), ("PED", "Pediatrics"))

RACES = (
    "White",
    "Black or African American",
    "Asian",
    "American Indian or Alaska Native",
    "Pacific Islander",
    "Other",
)

ETHNICITIES = ("Non-Hispanic or Latino", "Hispanic or Latino")

MARITAL = ("single", "married", "divorced", "widowed")
LANGUAGES = ("English", "English", "English", "Spanish", "Mandarin", "Vietnamese")

# relative summaries for lightweight FamilyMember rows: (conditions…, hereditary keys…)
RELATIVE_CONDITIONS = (
    ("type 2 diabetes", "E11.9", ("diabetes",)),
    ("hypertension", "I10", ("hypertension",)),
    ("coronary artery disease", "I25.10", ("hypertension",)),
    ("breast cancer", "C50.911", ()),
    ("colon cancer", "C18.9", ()),
    ("stroke", "I63.9", ("hypertension",)),
    ("COPD", "J44.9", ()),
)

# condition name → procedure performed at that visit (sometimes)
PROCEDURE_MAP = {
    "laceration": ("Laceration repair with sutures", 0.9),
    "sports_injury": ("Splint application", 0.4),
    "otitis_media": ("Cerumen removal", 0.1),
}

CHILDHOOD_VACCINES = (
    # (vaccine, cvx, doses, last-dose age in months)
    ("DTaP", "20", 5, 60),
    ("MMR", "03", 2, 60),
    ("IPV (polio)", "10", 4, 60),
    ("Hepatitis B", "08", 3, 18),
    ("Varicella", "21", 2, 60),
)


_issued_mrns: set[str] = set()


#: How long an ongoing prescription counts as current when it names no
#: duration. Matches the window `caregaps` and `careplan` use, so the three
#: cannot disagree about whether a patient is on a drug.
ONGOING_WINDOW_DAYS = 365

#: Classes where two concurrent drugs is normal practice rather than an
#: error, so the duplicate check must not intervene.
#:
#: Only one entry, and it is a real one: dual antiplatelet therapy — aspirin
#: with clopidogrel — is standard after a stent or an acute coronary
#: syndrome. Everything else in the formulary that has two drugs in a class
#: (statins, NSAIDs, PPIs, SSRIs, topical steroids) is a class where
#: concurrent use is a prescribing error.
CONCURRENT_CLASSES_OK = frozenset({"antiplatelet"})


def _class_root(drug_class: str | None) -> str:
    """The therapeutic class, without its qualifier.

    The formulary spells one class two ways: ``hyperlipidemia`` prescribes
    Atorvastatin as ``Statin`` and ``cad`` prescribes it as
    ``Statin (high-intensity)``. Both are true — high-intensity is a real
    distinction worth keeping in the data — but comparing the strings
    exactly makes them different classes, and a patient ends up on two
    statins because nothing recognised the second as a statin.

    So the qualifier stays in the record and comes off for the comparison.
    """
    root = (drug_class or "").split("(")[0]
    return root.strip().lower()


def _prescription_is_current(started, rx: dict, as_of) -> bool:
    """Is this prescription still running on ``as_of``?

    Delegates to :mod:`hdh.core.medications`, which is the one definition
    the readers share. This used to be its own copy, and the copies drifted:
    the generator respected duration and the two modules reading the chart
    did not (#115).
    """
    from .medications import is_current_row

    return is_current_row(rx, as_of, started=started, window_days=ONGOING_WINDOW_DAYS)


def _current_medications(rx_stream: list[tuple], as_of) -> tuple[set[str], set[str]]:
    """The classes and drug names running on ``as_of``, lowercased."""
    classes: set[str] = set()
    names: set[str] = set()
    for started, rx in rx_stream:
        if not _prescription_is_current(started, rx, as_of):
            continue
        klass = _class_root(rx.get("drug_class"))
        if klass:
            classes.add(klass)
        name = (rx.get("drug_name") or "").strip().lower()
        if name:
            names.add(name)
    return classes, names


def _would_duplicate_a_class(rx_spec, rx_stream: list[tuple], as_of) -> bool:
    """Would prescribing this start a second drug in a running class?

    Nobody is on two statins. Before this check, 27 of 178 generated
    patients (15%) were on a duplicated class — sixteen on two statins, five
    on two NSAIDs, one on two SSRIs — because each condition picked its drugs
    without ever looking at what the patient was already taking.

    A **repeat of the same drug is not a duplicate**: renewing a statin is
    the commonest event in primary care, and blocking it would leave a chart
    showing one prescription years ago and nothing since, which reads as
    having stopped.
    """
    klass = _class_root(getattr(rx_spec, "drug_class", None))
    if not klass or klass in CONCURRENT_CLASSES_OK:
        return False
    classes, names = _current_medications(rx_stream, as_of)
    if klass not in classes:
        return False
    return (getattr(rx_spec, "drug_name", "") or "").strip().lower() not in names


def _random_mrn() -> str:
    """A unique MRN — 8 random digits re-drawn on collision (the birthday
    paradox makes collisions likely by ~10k patients)."""
    while True:
        mrn = "MRN" + "".join(random.choices(string.digits, k=8))
        if mrn not in _issued_mrns:
            _issued_mrns.add(mrn)
            return mrn


def _height_cm(age: int, sex: str) -> float:
    if age < 2:
        return round(random.gauss(75, 5), 1)
    if age < 10:
        avg = 100 + (age - 2) * 6
        return round(random.gauss(avg, 4), 1)
    if sex == "M":
        return round(random.gauss(175, 8), 1) if age >= 18 else round(random.gauss(160, 10), 1)
    return round(random.gauss(162, 7), 1) if age >= 18 else round(random.gauss(155, 10), 1)


def _baseline_bmi(age: int) -> float:
    if age < 18:
        return round(random.gauss(20, 2), 1)
    return round(max(18.0, random.gauss(27.0, 4.5)), 1)


# ─── Providers ────────────────────────────────────────────────────────────────


def seed_providers(session) -> list[Provider]:
    """Create (or fetch) the practice's providers and specialties."""
    existing = session.query(Provider).all()
    if existing:
        return existing
    by_code = {}
    for code, name in SPECIALTIES:
        spec = Specialty(code=code, name=name)
        session.add(spec)
        by_code[code] = spec
    session.flush()
    providers = []
    for identifier, name, spec_code in PRACTICE:
        provider = Provider(identifier=identifier, name=name, specialty=by_code[spec_code])
        session.add(provider)
        providers.append(provider)
    session.flush()
    return providers


def _primary_provider(providers: Sequence[Provider], age: int) -> Provider:
    """Continuity of care: children see the pediatrician when there is one."""
    if age < 13:
        peds = [p for p in providers if p.specialty and p.specialty.code == "PED"]
        if peds and random.random() < 0.7:
            return random.choice(peds)
    return random.choice([p for p in providers if not p.specialty or p.specialty.code != "PED"])


# ─── Patient generation ───────────────────────────────────────────────────────


def generate_patient(
    age: int,
    surname: str | None = None,
    address: tuple[str, str, str, str] | None = None,
    *,
    as_of: date | None = None,
) -> Patient:
    """Generate one synthetic patient of a given age (household-aware)."""
    as_of = as_of or TODAY
    sex = random.choice(["M", "F"])
    dob = as_of - timedelta(days=age * 365 + random.randint(0, 364))
    fname = fake.first_name_male() if sex == "M" else fake.first_name_female()
    street, city, state, zipc = address or (
        fake.street_address(),
        fake.city(),
        fake.state_abbr(),
        fake.zipcode(),
    )
    return Patient(
        mrn=_random_mrn(),
        first_name=fname,
        last_name=surname or fake.last_name(),
        date_of_birth=dob,
        sex=sex,
        race=random.choice(RACES),
        ethnicity=random.choices(ETHNICITIES, weights=[80, 20], k=1)[0],
        address=street,
        city=city,
        state=state,
        zip_code=zipc,
        phone=fake.phone_number()[:15],
        email=fake.email() if age >= 14 else "",
        insurance_name=random.choice(INSURERS),
        insurance_id=fake.bothify("???-########"),
        blood_type=random.choice(BLOOD_TYPES),
        marital_status=(random.choices(MARITAL, weights=[30, 50, 12, 8], k=1)[0] if age >= 25 else "single"),
        language=random.choice(LANGUAGES),
        smoker=age >= 18 and random.random() < 0.15,
        bmi_baseline=_baseline_bmi(age),
    )


def generate_allergies(session, patient: Patient) -> list[str]:
    """0–2 structured allergies (~40% of patients); returns the substances."""
    n = random.choices([0, 1, 2], weights=[60, 30, 10], k=1)[0]
    substances = []
    for substance, reaction, severity in random.sample(ALLERGY_SPECS, n):
        session.add(
            Allergy(
                patient_id=patient.id,
                substance=substance,
                reaction=reaction,
                severity=AllergySeverity(severity),
                noted_date=patient.date_of_birth + timedelta(days=random.randint(365, 365 * 20)),
            )
        )
        substances.append(substance)
    return substances


def generate_extended_relatives(session, patient: Patient, age: int) -> tuple[dict[str, bool], list[str]]:
    """Lightweight relatives with narrative summaries + FamilyHistory rows.

    Returns (hereditary-risk flags, display lines) — the flags feed
    comorbidity_seeds, so family history has real consequences; the lines
    feed in-memory note rendering.
    """
    hereditary: dict[str, bool] = {}
    lines: list[str] = []
    if age < 18:
        return hereditary, lines
    relationships = ["father", "mother"]
    if random.random() < 0.4:
        relationships.append(random.choice(["brother", "sister"]))
    for rel in relationships:
        conditions = random.sample(
            RELATIVE_CONDITIONS, random.choices([0, 1, 2], weights=[35, 45, 20], k=1)[0]
        )
        deceased = rel in ("father", "mother") and random.random() < (0.5 if age > 50 else 0.15)
        deceased_age = random.randint(60, 92) if deceased else None
        cond_text = ", ".join(c[0] for c in conditions) if conditions else "no significant conditions"
        summary = (
            f"{rel.title()} lived to {deceased_age}; history of {cond_text}; died of natural causes."
            if deceased
            else f"{rel.title()}, alive; history of {cond_text}."
        )
        member = FamilyMember(
            patient_id=patient.id,
            relationship_type=rel,
            name=f"{fake.first_name()} {patient.last_name}",
            deceased=deceased,
            deceased_age=deceased_age,
            summary=summary,
        )
        session.add(member)
        session.flush()
        for cond_name, icd10, keys in conditions:
            session.add(
                FamilyHistory(
                    patient_id=patient.id,
                    family_member_id=member.id,
                    relationship_type=rel,
                    condition=cond_name,
                    icd10_code=icd10,
                    onset_age=random.randint(35, 70),
                )
            )
            for key in keys:
                hereditary[key] = True
            lines.append(f"{rel}: {cond_name}")
    return hereditary, lines


def link_household(session, members: list[Patient]) -> None:
    """FamilyMember rows between household patients + emergency contacts."""
    adults = [m for m in members if m.age >= 18]
    for person in members:
        contact_member_id = None
        for other in members:
            if other.id == person.id:
                continue
            if person.age >= 18 and other.age >= 18:
                rel = "spouse"
            elif person.age >= 18:
                rel = "child"
            elif other.age >= 18:
                rel = (
                    random.choice(["mother", "father"])
                    if other.sex is None
                    else ("mother" if str(other.sex).endswith("F") else "father")
                )
            else:
                rel = "sibling"
            fm = FamilyMember(
                patient_id=person.id,
                relationship_type=rel,
                name=f"{other.first_name} {other.last_name}",
                date_of_birth=other.date_of_birth,
                related_patient_id=other.id,
                phone=other.phone,
            )
            session.add(fm)
            session.flush()
            if contact_member_id is None and (
                rel == "spouse" or (person.age < 18 and rel in ("mother", "father"))
            ):
                contact_member_id = fm.id
        if contact_member_id is None and adults and person.age < 18:
            pass  # parent link above always exists for children in multi-member households
        person.emergency_contact_id = contact_member_id


def hereditary_from_patients(
    parents: list[Patient], parent_conditions: dict[int, set], catalog: ConditionCatalog
) -> dict[str, bool]:
    """Derive a child's hereditary-risk flags from parents' ACTUAL conditions."""
    flags: dict[str, bool] = {}
    for parent in parents:
        for cname in sorted(parent_conditions.get(parent.id, set())):
            onset = catalog.get(cname).onset
            if onset is not None and onset.hereditary_key:
                flags[onset.hereditary_key] = True
    return flags


def record_parent_history(
    session,
    child: Patient,
    parents: list[Patient],
    parent_conditions: dict[int, set],
    catalog: ConditionCatalog,
) -> list[str]:
    """FamilyHistory rows on the child for each parent's chronic conditions;
    returns display lines for note rendering."""
    lines: list[str] = []
    for parent in parents:
        rel = "mother" if str(parent.sex).endswith("F") else "father"
        # Sorted for the same reason as `onset_dates`: this loop both draws
        # from the RNG and appends the lines a note renders, so hash order
        # made a child's family history read differently run to run.
        for cname in sorted(parent_conditions.get(parent.id, set())):
            profile = catalog.get(cname)
            if profile:
                session.add(
                    FamilyHistory(
                        patient_id=child.id,
                        relationship_type=rel,
                        condition=profile.description,
                        icd10_code=profile.icd10_code,
                        onset_age=max(25, parent.age - random.randint(0, 15)),
                    )
                )
                lines.append(f"{rel}: {profile.description}")
    return lines


# ─── Vitals / labs (unchanged mechanics) ─────────────────────────────────────


def _baseline_vitals(age: int, sex: str, bmi: float):
    """Return baseline (healthy) vitals for this patient."""
    sys_base = 110 + min(age // 4, 20) + (5 if sex == "M" else 0)
    dia_base = 70 + min(age // 6, 12)
    return sys_base, dia_base, 72, 16, 98.6, 98


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def generate_vital(visit_id: int, age: int, sex: str, bmi: float, condition_profile) -> Vital:
    """Generate a vitals panel: age/sex baseline plus the condition's deltas."""
    cp = condition_profile
    sys_b, dia_b, hr_b, rr_b, temp_b, spo2_b = _baseline_vitals(age, sex, bmi)

    bp_sys = int(_clamp(random.gauss(sys_b + cp.bp_sys_delta[0], cp.bp_sys_delta[1]), 85, 220))
    bp_dia = int(_clamp(random.gauss(dia_b + cp.bp_dia_delta[0], cp.bp_dia_delta[1]), 50, 130))
    hr = int(_clamp(random.gauss(hr_b + cp.hr_delta[0], cp.hr_delta[1]), 45, 160))
    rr = int(_clamp(random.gauss(rr_b + cp.rr_delta[0], cp.rr_delta[1]), 10, 40))
    temp = round(_clamp(random.gauss(temp_b + cp.temp_delta[0], cp.temp_delta[1]), 96.0, 105.0), 1)
    spo2 = int(_clamp(random.gauss(spo2_b + cp.spo2_delta[0], cp.spo2_delta[1]), 80, 100))

    bmi_v = round(max(12.0, random.gauss(bmi, 0.8)), 1)
    ht_cm = _height_cm(age, sex)
    wt_kg = round(bmi_v * (ht_cm / 100) ** 2, 1)
    pain = int(_clamp(round(random.gauss(cp.pain[0], cp.pain[1])), 0, 10))

    return Vital(
        visit_id=visit_id,
        bp_systolic=bp_sys,
        bp_diastolic=bp_dia,
        heart_rate=hr,
        respiratory_rate=rr,
        temperature_f=temp,
        oxygen_sat=spo2,
        weight_kg=wt_kg,
        height_cm=ht_cm,
        bmi=bmi_v,
        pain_scale=pain,
    )


def generate_lab(visit_id: int, spec: LabSpec, has_condition: bool = True) -> LabResult:
    """Generate one lab result from its spec, shifted when the condition is present."""
    if has_condition and spec.condition_shift != 0:
        val = random.gauss(spec.normal_mean + spec.condition_shift, spec.condition_shift_sd or spec.normal_sd)
    else:
        val = random.gauss(spec.normal_mean, spec.normal_sd)

    val = round(val, 2)

    if val < spec.ref_low:
        status = LabStatus.LOW
    elif val > spec.ref_high:
        status = LabStatus.HIGH
    else:
        status = LabStatus.NORMAL
    if spec.test_name == "Glucose (stat)" and val > 400:
        status = LabStatus.CRITICAL
    if spec.test_name == "WBC" and val > 20:
        status = LabStatus.CRITICAL

    return LabResult(
        visit_id=visit_id,
        test_name=spec.test_name,
        value=val,
        unit=spec.unit,
        reference_low=spec.ref_low,
        reference_high=spec.ref_high,
        status=status,
        loinc_code=spec.loinc_code,
    )


# ─── Visit history ────────────────────────────────────────────────────────────


def _visits_per_year(age: int, established_conditions: set) -> float:
    """Estimate average visits/year for this patient profile."""
    base = 2.0
    if age < 3:
        base = 5.0
    elif age < 18:
        base = 2.5
    elif age > 65:
        base = 5.0
    elif age > 45:
        base = 3.5
    base += len(established_conditions) * 0.9
    return base


def _bmi(patient: Patient) -> float:
    if patient.bmi_baseline is None:
        raise ValueError(f"patient {patient.mrn} has no bmi_baseline")
    return patient.bmi_baseline


@dataclass(frozen=True)
class HistoryResult:
    """One patient's generated history: the visit triples, the final
    chronic set, when each rolled-onset condition actually began, and
    each staged condition's final severity stage."""

    visits: tuple
    established: frozenset[str]
    onset_dates: dict[str, date]
    final_stages: dict[str, Stage]


def generate_visit_history(patient: Patient, fam_hx: dict, smoker: bool, scope: RunScope) -> HistoryResult:
    """Generate a realistic multi-year visit history for a patient.

    Two-phase chronic onset (design clinical-breadth.md §5): baseline
    seeding at chart start, then ANNUAL onset rolls interleaved with the
    visit timeline — the comorbidity webs multiply the rates, so CKD
    arrives after (and because of) the hypertension years. Staged
    conditions evolve on the run's cadence; the roll date becomes the
    condition's clinical onset date."""
    years = scope.years
    age_at_start = max(0, patient.age - years)
    family_keys = frozenset(key for key, present in fam_hx.items() if present)

    def ctx(age: int, month: int, established: frozenset) -> SamplingContext:
        return SamplingContext(
            age=age,
            sex=patient.sex,
            month=month,
            established=established,
            family_history=family_keys,
            smoker=smoker,
            bmi=_bmi(patient),
            rng=scope.rng,
        )

    established = {profile.name for profile in scope.catalog.seed_chronic(ctx(patient.age, 1, frozenset()))}
    # `sorted`, not `established`, every time the order reaches an output or
    # the RNG. A set of strings iterates in hash order, and Python randomises
    # string hashing per process — so the same seed drew a different onset
    # date for each condition on every run, and the chart the eval cohort
    # claims to rebuild from seed 4242 was not the same chart twice.
    stage_index: dict[str, int] = {
        name: staging.start_index
        for name in sorted(established)
        if (staging := scope.catalog.get(name).staging) is not None
    }

    start_date = scope.as_of - timedelta(days=years * 365)
    # Baseline-seeded conditions predate the chart window: the patient
    # ARRIVED with them, so their clinical onset lands before it (first
    # visit merely records them) — this is what keeps rolled onsets like
    # CKD chronologically AFTER their drivers.
    onset_dates: dict[str, date] = {
        name: start_date - timedelta(days=scope.rng.randint(180, 365 * 6)) for name in sorted(established)
    }
    all_visits = []

    visits_per_yr = _visits_per_year(patient.age, established)
    total_visits = max(1, int(random.gauss(visits_per_yr * years, years * 0.5)))
    total_visits = _clamp(total_visits, 1, 80)

    visit_dates = sorted(
        random.sample(
            [start_date + timedelta(days=d) for d in range(years * 365)], k=min(total_visits, years * 365)
        )
    )

    period_days = 365 // scope.periods_per_year
    total_periods = years * scope.periods_per_year
    next_period = 1

    def roll_period(period: int) -> None:
        """One cadence boundary: staging steps; yearly boundaries also
        roll new chronic onsets through the comorbidity webs."""
        boundary = start_date + timedelta(days=period * period_days)
        boundary_age = age_at_start + (boundary - start_date).days // 365
        if period % scope.periods_per_year == 0:
            for profile in scope.catalog.annual_onsets(
                ctx(boundary_age, boundary.month, frozenset(established))
            ):
                established.add(profile.name)
                onset_dates[profile.name] = boundary
                if profile.staging is not None:
                    stage_index[profile.name] = profile.staging.start_index
        for name in list(stage_index):
            staging = scope.catalog.get(name).staging
            if staging is not None:
                stage_index[name] = staging.step(stage_index[name], scope.rng, scope.periods_per_year)

    for vdate in visit_dates:
        day = (vdate - start_date).days
        while next_period <= total_periods and day >= next_period * period_days:
            roll_period(next_period)
            next_period += 1
        visit_age = age_at_start + day // 365
        cprofile = scope.catalog.sample_visit_condition(ctx(visit_age, vdate.month, frozenset(established)))
        visit = Visit(
            patient_id=patient.id,
            visit_date=vdate,
            visit_type=cprofile.visit_type,
            chief_complaint=cprofile.chief_complaint,
        )
        all_visits.append((visit, cprofile, cprofile.name))

        if cprofile.chronic:
            established.add(cprofile.name)
            if cprofile.staging is not None and cprofile.name not in stage_index:
                stage_index[cprofile.name] = cprofile.staging.start_index

    while next_period <= total_periods:  # stages keep evolving after the last visit
        roll_period(next_period)
        next_period += 1

    final_stages = {
        name: staging.stages[index]
        for name, index in stage_index.items()
        if (staging := scope.catalog.get(name).staging) is not None
    }
    return HistoryResult(
        visits=tuple(all_visits),
        established=frozenset(established),
        onset_dates=onset_dates,
        final_stages=final_stages,
    )


def _emit_conditions(
    session, patient: Patient, visit: Visit, cprofile, chronic_seen: dict, history=None
) -> None:
    """Unified problem list: acute visits get resolved encounter conditions;
    chronic conditions get ONE active row at first diagnosis. A rolled
    onset (annual comorbidity web) supplies the CLINICAL onset date —
    the disease began before the visit that records it."""
    if cprofile.chronic:
        if cprofile.name not in chronic_seen:
            onset = (
                history.onset_dates.get(cprofile.name) if history is not None else None
            ) or visit.visit_date
            cond = Condition(
                patient_id=patient.id,
                visit_id=visit.id,
                icd10_code=cprofile.icd10_code,
                description=cprofile.description,
                chronic=True,
                status=ConditionStatus.ACTIVE,
                controlled=random.random() > 0.25,
                onset_date=onset,
            )
            session.add(cond)
            session.flush()
            chronic_seen[cprofile.name] = cond
    else:
        duration = random.randint(7, 30)
        session.add(
            Condition(
                patient_id=patient.id,
                visit_id=visit.id,
                icd10_code=cprofile.icd10_code,
                description=cprofile.description,
                chronic=False,
                status=ConditionStatus.RESOLVED,
                onset_date=visit.visit_date,
                resolved_date=visit.visit_date + timedelta(days=duration),
            )
        )


def _procedures_for(patient: Patient, visit, cname: str) -> list[dict]:
    """Procedure rows implied by this visit's condition (in-memory)."""
    rows = []
    spec = PROCEDURE_MAP.get(cname)
    if spec and random.random() < spec[1]:
        rows.append(
            {
                "patient_id": patient.id,
                "visit_id": visit.id,
                "description": spec[0],
                "performed_date": visit.visit_date,
                "provider_id": visit.provider_id,
            }
        )
    if "preventive" in str(visit.visit_type).lower() and patient.age >= 50 and random.random() < 0.1:
        rows.append(
            {
                "patient_id": patient.id,
                "visit_id": visit.id,
                "description": "Screening colonoscopy",
                "performed_date": visit.visit_date,
                "provider_id": visit.provider_id,
            }
        )
    return rows


def generate_immunizations(session, patient: Patient, chronic: set, *, as_of: date | None = None) -> None:
    """Age-appropriate immunization history (simplified CDC shape)."""
    rows: list[dict] = []
    age = patient.age
    for vaccine, cvx, doses, last_month in CHILDHOOD_VACCINES:
        given = min(doses, max(0, int(doses * min(1.0, (age * 12) / last_month))))
        for dose in range(1, given + 1):
            offset_days = int((last_month / doses) * dose * 30.4)
            rows.append(
                {
                    "patient_id": patient.id,
                    "vaccine": vaccine,
                    "cvx_code": cvx,
                    "administered_date": patient.date_of_birth + timedelta(days=offset_days),
                    "dose_number": dose,
                }
            )
    # Annual flu shots for seniors and chronic patients (last few seasons, ~70% uptake)
    if age >= 65 or chronic:
        for years_back in range(1, 4):
            if random.random() < 0.7:
                season = (as_of or TODAY).year - years_back
                rows.append(
                    {
                        "patient_id": patient.id,
                        "vaccine": "Influenza, seasonal",
                        "cvx_code": "141",
                        "administered_date": date(season, random.randint(9, 11), random.randint(1, 28)),
                        "dose_number": 1,
                    }
                )
    # Td booster roughly every 10 years for adults
    if age >= 19:
        rows.append(
            {
                "patient_id": patient.id,
                "vaccine": "Td (tetanus, diphtheria)",
                "cvx_code": "139",
                "administered_date": (as_of or TODAY) - timedelta(days=random.randint(0, 3650)),
                "dose_number": 1,
            }
        )
    if rows:
        session.execute(sa_insert(Immunization), rows)


def generate_medication_statements(
    session, patient: Patient, chronic_seen: dict, rx_stream: list[tuple]
) -> None:
    """Derive the cross-visit medication list from the in-memory rx stream
    (list of (visit_date, rx_dict)) — zero relationship traversal."""
    seen: dict[str, dict] = {}
    for visit_date, rx in rx_stream:
        stmt = seen.get(rx["drug_name"])
        if stmt is None:
            is_chronic_drug = rx["duration_days"] is None
            indication = None
            if is_chronic_drug:
                for cond in chronic_seen.values():
                    indication = cond.id
                    break
            seen[rx["drug_name"]] = {
                "patient_id": patient.id,
                "drug_name": rx["drug_name"],
                "drug_class": rx["drug_class"],
                "dose": rx["dose"],
                "frequency": rx["frequency"],
                "status": MedicationStatus.ACTIVE if is_chronic_drug else MedicationStatus.COMPLETED,
                "start_date": visit_date,
                "end_date": (
                    None if is_chronic_drug else visit_date + timedelta(days=rx["duration_days"] or 10)
                ),
                "indication_id": indication,
            }
        elif stmt["end_date"] and stmt["status"] == MedicationStatus.COMPLETED:
            stmt["end_date"] = visit_date + timedelta(days=rx["duration_days"] or 10)
    if seen:
        session.execute(sa_insert(MedicationStatement), list(seen.values()))


# ─── Full dataset builder ─────────────────────────────────────────────────────


def _household_sizes(n_patients: int) -> list[int]:
    sizes = []
    remaining = n_patients
    while remaining > 0:
        size = random.choices([1, 2, 3, 4, 5], weights=[30, 30, 18, 15, 7], k=1)[0]
        size = min(size, remaining)
        sizes.append(size)
        remaining -= size
    return sizes


#: A resident child is a minor (0–17) whose parent is 20–40 years older,
#: so a household that HAS one needs a parent aged 20–57. Deciding the
#: composition before the parent's age is what keeps that true.
_PARENT_GAP = (20, 40)
_CHILD_AGES = (0, 17)


def _household_ages(size: int) -> list[int]:
    """Coherent ages: adults first, children 20–40 years younger.

    The composition is decided BEFORE the parent's age, because the two
    constrain each other. Clamping the child instead — which is what this
    did — produced a 17-year-old with a 66-year-old "father": the clamp
    silently stretched the gap to 49 years, and the household-coherence
    test rightly rejected it.
    """
    if size == 1:
        return [random.choice([random.randint(18, 65), random.randint(66, 90)])]

    spouse = random.random() < 0.75
    children = max(0, size - 1 - (1 if spouse else 0))
    # EVERY adult in the household is a candidate parent — the relationship
    # pass gives a minor a mother/father from whichever adults are there —
    # so the gap has to hold for the youngest and the oldest of them, not
    # just for the one we happened to draw first. A spouse may be six years
    # older, hence the 51 rather than 57.
    parent_age = random.randint(28, 51 if children else 62)

    ages = [parent_age]
    if spouse:
        ages.append(_clamp(parent_age + random.randint(-6, 6), 20, 90))

    adults = [age for age in ages if age >= 18]
    for _ in range(children):
        youngest = max(_CHILD_AGES[0], max(adults) - _PARENT_GAP[1])
        oldest = min(_CHILD_AGES[1], min(adults) - _PARENT_GAP[0])
        ages.append(random.randint(youngest, oldest))
    return ages[:size]


def _row(obj, model) -> dict:
    """Extract a bulk-insert dict from a transient ORM object (skip null pk)."""
    return {
        c.name: getattr(obj, c.name)
        for c in model.__table__.columns
        if not (c.primary_key and getattr(obj, c.name) is None)
    }


def _referral_request(patient, visit, target: str):
    """A referral is an ORDER, not a prescription (#49, design §5).

    It used to be an ``RxSpec`` with an em-dash dose and frequency, which
    is how "Lifestyle counseling referral" ended up in a patient's drug
    list. As a request it says what it is, and a real referral to
    cardiology would look the same.
    """
    return ServiceRequest(
        patient_id=patient.id,
        visit_id=visit.id,
        requester_id=visit.provider_id,
        kind=ServiceKind.REFERRAL,
        status=RequestStatus.ACTIVE,
        origin=RequestOrigin.GENERATED,
        display=target,
        requested_date=visit.visit_date,
        detail={"specialty": target},
    )


def _served_request(patient, visit, kind, display, *, code_system=None, code=None) -> dict:
    """An order that was placed and served in the same encounter.

    The generator writes charts for events that already happened, so most of
    its requests are raised and answered at once — a lab drawn at the visit,
    a procedure performed during it. That is still a request followed by a
    fulfilment rather than a fact appearing from nowhere
    (`requests-and-read-models.md`), and it is what gives the result
    something to point at.

    `status` and `end_date` are set together, because a served request that
    does not say *when* it closed looks open to everything that reads dates.
    """
    return {
        "patient_id": patient.id,
        "visit_id": visit.id,
        "requester_id": visit.provider_id,
        "kind": ServiceKind.LAB if kind == "LAB" else getattr(ServiceKind, kind),
        "status": RequestStatus.COMPLETED,
        "origin": RequestOrigin.GENERATED,
        "display": display[:200],
        "code_system": code_system,
        "code": code,
        "requested_date": visit.visit_date,
        "occurrence_date": visit.visit_date,
        "end_date": visit.visit_date,
    }


def _follow_up_request(patient, visit, days: int):
    """The generated order behind "return in N days" (issue #59).

    A follow-up used to be an integer on the visit that nobody could
    explain the provenance of. As a request it carries an origin, it shows
    up in `hdh orders list` beside labs and medications, and a clinician can
    move it through the audited edit path like any other order.

    `occurrence_date` holds the interval, so `Visit.follow_up_days` reads
    back exactly what was asked for.
    """
    from datetime import timedelta

    return ServiceRequest(
        patient_id=patient.id,
        visit_id=visit.id,
        requester_id=visit.provider_id,
        kind=ServiceKind.FOLLOW_UP,
        status=RequestStatus.ACTIVE,
        origin=RequestOrigin.GENERATED,
        display=f"Follow-up visit in {days} days",
        requested_date=visit.visit_date,
        occurrence_date=visit.visit_date + timedelta(days=days),
    )


@dataclass(frozen=True)
class NoteFacts:
    """Patient-level facts the note renderer needs (built in memory)."""

    allergies: tuple[str, ...] = ()
    family_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderBuffers:
    """The row accumulators prescribing writes into, passed as one thing.

    They travel together because they are one transaction seen from four
    sides: the order, the authorisation, the supply that answers it, and the
    running medication list the duplicate-class guard reads. Splitting them
    across a parameter list invites a caller to pass three of the four.
    """

    requests: list[dict]
    prescriptions: list[dict]
    dispenses: list[dict]
    history: list[tuple]


class VisitOrders(NamedTuple):
    """What a visit's formulary picks turned into.

    Three different things come out of one list, and only one of them is a
    medication (#49): a referral becomes an order, and advice belongs in the
    note and NOWHERE in the chart — charting it made the record claim the
    patient had been prescribed "Rest & fluids".
    """

    prescriptions: list[dict]
    referrals: list[str]
    advice: list[str]


#: How long a repeat authorisation stays valid when nothing else bounds it.
#: A year is the usual review interval for a chronic repeat, and it is also
#: what stops a generated order being refillable forever.
AUTHORISATION_DAYS = 365

#: How often a repeat is collected when no duration says otherwise.
DEFAULT_SUPPLY_DAYS = 30

#: Chance a patient collects the next refill they are entitled to.
#:
#: Not every authorised refill is taken, and a chart where all of them are
#: would be a chart with no non-adherence in it — which is the thing a care
#: plan most often has to notice. Collection stops at the first miss rather
#: than resuming, so a lapse looks like a lapse.
REFILL_COLLECTED = 0.85


def _medication_order(patient, visit, rx_spec) -> dict:
    """The authorisation behind a prescription, with what it permits.

    Two departures from :func:`_served_request`, both because an
    authorisation is not a one-shot order:

    **An order with refills left is not over.** `_served_request` closes
    what it creates, which is right for a lab drawn at the visit and wrong
    here: `end_date` says the request's life has ended, and an authorisation
    the patient may still draw on has not ended. Measured before this
    changed: 0 of 949 generated medication orders were refillable, so the
    refill tool would have refused every request ever made against a
    generated chart.

    **`valid_until` bounds it in time.** A repeat that never expires is one
    nobody has to review, which is not how prescribing works.
    """
    refills = int(getattr(rx_spec, "refills", 0) or 0)
    duration = getattr(rx_spec, "duration_days", None)
    if duration:
        # A course: valid for as long as its fills could reasonably run.
        valid_days = int(duration) * (refills + 1)
    else:
        valid_days = AUTHORISATION_DAYS

    order = _served_request(
        patient,
        visit,
        "MEDICATION",
        rx_spec.drug_name,
        code_system="rxnorm" if rx_spec.rxcui else None,
        code=rx_spec.rxcui,
    )
    order["refills_authorised"] = refills or None
    order["valid_until"] = visit.visit_date + timedelta(days=valid_days)
    if refills:
        order["status"] = RequestStatus.ACTIVE
        order["end_date"] = None
    return order


def _repeat_fills(patient, visit, rx_spec, request_index: int, as_of) -> list[dict]:
    """The refills the patient actually collected after the first supply.

    Uses its own RNG, seeded from the patient, the visit and the drug rather
    than drawn from the run's stream. That is deliberate: a new draw against
    the shared RNG would shift every subsequent value and change the whole
    chart, so adding refills would have re-baselined the cohort for reasons
    that have nothing to do with refills. Seeding from a string is stable
    across processes — `Random` hashes it with SHA-512 rather than `hash()`.
    """
    refills = int(getattr(rx_spec, "refills", 0) or 0)
    if not refills:
        return []

    supply = int(getattr(rx_spec, "duration_days", None) or DEFAULT_SUPPLY_DAYS)
    expires = visit.visit_date + timedelta(days=supply * (refills + 1))
    rng = random.Random(f"{patient.id}-{visit.id}-{rx_spec.drug_name}-refills")

    fills = []
    when = visit.visit_date
    for _ in range(refills):
        when = when + timedelta(days=supply)
        if when > as_of or when > expires:
            break
        if rng.random() > REFILL_COLLECTED:
            # A missed collection ends the run rather than skipping one. A
            # patient who stops collecting usually stops, and a gap followed
            # by resumption would need a reason the generator does not have.
            break
        fills.append(
            {
                "_request": request_index,
                "patient_id": patient.id,
                "drug_name": rx_spec.drug_name,
                "dispensed_date": when,
                "days_supply": supply,
                "origin": "GENERATED",
            }
        )
    return fills


def _prescribe_at_visit(
    patient,
    visit,
    cprofile,
    *,
    is_chronic: bool,
    buffers: OrderBuffers,
    as_of: date,
) -> VisitOrders:
    """Decide what this visit prescribes, and what answers each order.

    Appends to the caller's accumulators rather than returning rows: the
    `_request` index a read model carries is the position a request will
    occupy, so the request list has to be the shared one.
    """
    orders = VisitOrders([], [], [])
    if not cprofile.rx_options:
        return orders

    if cprofile.rx_pick_all:
        rx_list = cprofile.rx_options
    else:
        n_rx = random.choices([1, 2], weights=[75, 25], k=1)[0]
        rx_list = random.sample(cprofile.rx_options, k=min(n_rx, len(cprofile.rx_options)))

    for rx_spec in rx_list:
        if rx_spec.kind is RxKind.ADVICE:
            orders.advice.append(rx_spec.drug_name)
            continue
        if rx_spec.kind is RxKind.REFERRAL:
            orders.referrals.append(rx_spec.drug_name)
            buffers.requests.append(
                _row(_referral_request(patient, visit, rx_spec.drug_name), ServiceRequest)
            )
            continue
        # A patient already on a statin does not get a second one. Nothing
        # here previously looked at the medication list, so each condition
        # prescribed in ignorance of the others.
        if _would_duplicate_a_class(rx_spec, buffers.history, visit.visit_date):
            continue
        rx = {
            "visit_id": visit.id,
            "drug_name": rx_spec.drug_name,
            "drug_class": rx_spec.drug_class,
            "dose": rx_spec.dose,
            "frequency": rx_spec.frequency,
            "duration_days": rx_spec.duration_days,
            "refills": rx_spec.refills,
            "is_new": not is_chronic or random.random() > 0.5,
            # a formulary entry that knows its drug passes the code on
            "code_system": "rxnorm" if rx_spec.rxcui else None,
            "code": rx_spec.rxcui,
        }
        # The authorisation, and the supply that answers it. The prescription
        # stays exactly what it was — the line written at this encounter —
        # while the dispense is what says the medication actually reached the
        # patient.
        rx["_request"] = len(buffers.requests)
        buffers.requests.append(_medication_order(patient, visit, rx_spec))
        buffers.dispenses.append(
            {
                "_request": rx["_request"],
                "patient_id": patient.id,
                "drug_name": rx_spec.drug_name,
                "dispensed_date": visit.visit_date,
                "days_supply": rx_spec.duration_days,
                "origin": "GENERATED",
                "visit_id": visit.id,
            }
        )
        # And the refills collected against it afterwards. These are what
        # make an authorisation observable: a chart with one fill per
        # prescription cannot show a patient who stopped collecting.
        buffers.dispenses.extend(_repeat_fills(patient, visit, rx_spec, rx["_request"], as_of))
        buffers.prescriptions.append(rx)
        orders.prescriptions.append(rx)
        buffers.history.append((visit.visit_date, rx))

    return orders


def _place_requests(
    session,
    request_rows: list[dict],
    read_models: tuple[list[dict], ...],
    follow_ups: list[tuple],
    visits: list,
) -> None:
    """Insert the intents, then point the facts at them.

    Requests go in **first**. A read model is written only as the outcome of
    a fulfilment (`requests-and-read-models.md`), so an intent has to exist —
    and have an id — before the fact that answers it. Rows carry a temporary
    `_request` index rather than an id, because ids do not exist until this
    call; `sort_by_parameter_order` is what makes the returned ids line up
    with the rows that asked for them.
    """
    if not request_rows:
        return

    request_ids = list(
        session.execute(
            sa_insert(ServiceRequest).returning(ServiceRequest.id, sort_by_parameter_order=True),
            request_rows,
        ).scalars()
    )
    for rows in read_models:
        for pending in rows:
            index = pending.pop("_request", None)
            if index is not None:
                pending["request_id"] = request_ids[index]

    # A follow-up is answered by the next visit on or after the date it asked
    # for. Attending is the evidence, so the visit carries the link and the
    # request closes on the day it happened. A follow-up nobody returned for
    # stays open, which is the truth about it.
    later = sorted(visits, key=lambda v: v.visit_date)
    for index, asked_on, days in follow_ups:
        due = asked_on + timedelta(days=days)
        answered = next((v for v in later if v.visit_date >= due and v.request_id is None), None)
        if answered is None:
            # Either nobody came back, or the visit that would have answered
            # this is already recorded as answering another follow-up. A
            # visit can genuinely answer several, and `Visit.request_id` can
            # name one — so the rest stay open rather than being marked
            # fulfilled with no evidence to point at. Claiming a fulfilment
            # the chart cannot show is the thing this layer exists to stop.
            continue
        answered.request_id = request_ids[index]
        session.execute(
            sa_update(ServiceRequest)
            .where(ServiceRequest.id == request_ids[index])
            .values(status=RequestStatus.COMPLETED, end_date=answered.visit_date)
        )


def _generate_one(
    session,
    patient: Patient,
    fam_hx: dict,
    scope: RunScope,
    facts: NoteFacts | None = None,
) -> tuple[int, set]:
    """Visits, conditions, notes, and per-patient chart entities.

    The hot path: ONE flush for all visits, notes rendered from the
    in-memory objects (zero lazy loads), children bulk-inserted.
    Returns (visit count, final chronic set)."""
    from .notes import render_soap

    facts = facts or NoteFacts()

    providers = scope.providers
    primary = _primary_provider(providers, patient.age)
    history = generate_visit_history(patient, fam_hx, bool(patient.smoker), scope)
    visit_tuples, final_chronic = history.visits, set(history.established)

    for visit, _cprofile, _cname in visit_tuples:
        visit.patient_id = patient.id
        visit.provider_id = (primary if random.random() < 0.8 else random.choice(providers)).id
    session.add_all([v for v, _, _ in visit_tuples])
    session.flush()  # the ONE flush: every visit now has its id
    provider_names = {pr.id: pr.name for pr in providers}

    chronic_seen: dict = {}
    vital_rows: list[dict] = []
    rx_rows: list[dict] = []
    lab_rows: list[dict] = []
    proc_rows: list[dict] = []
    note_rows: list[dict] = []
    request_rows: list[dict] = []
    rx_stream: list[tuple] = []
    dispense_rows: list[dict] = []
    # One handle on the four lists prescribing writes into; they are the same
    # objects, so everything downstream still reads them directly.
    buffers = OrderBuffers(request_rows, rx_rows, dispense_rows, rx_stream)
    follow_ups: list[tuple] = []
    sex_word = "male" if str(patient.sex).endswith("M") else "female"

    for visit, cprofile, cname in visit_tuples:
        vital = generate_vital(visit.id, patient.age, patient.sex, _bmi(patient), cprofile)
        vital_rows.append(_row(vital, Vital))

        if cprofile.follow_up_days:
            # Remembered so the visit that answers it can point back. A
            # follow-up is the one kind whose fulfilment is an event we also
            # generate, so the link is knowable rather than guessed.
            follow_ups.append((len(request_rows), visit.visit_date, cprofile.follow_up_days))
            request_rows.append(
                _row(_follow_up_request(patient, visit, cprofile.follow_up_days), ServiceRequest)
            )

        _emit_conditions(session, patient, visit, cprofile, chronic_seen, history)

        visit_rx, visit_referrals, visit_advice = _prescribe_at_visit(
            patient,
            visit,
            cprofile,
            is_chronic=cname in final_chronic,
            buffers=buffers,
            as_of=scope.as_of,
        )

        visit_labs = [generate_lab(visit.id, spec, has_condition=True) for spec in cprofile.labs]
        for lab in visit_labs:
            lab_row = _row(lab, LabResult)
            # The order this result answers. Index rather than id: requests
            # are inserted first and their ids stamped on afterwards, because
            # a read model may not be written without the request it fulfils.
            lab_row["_request"] = len(request_rows)
            request_rows.append(_served_request(patient, visit, "LAB", f"{lab.test_name}", code_system=None))
            lab_rows.append(lab_row)

        procedures = _procedures_for(patient, visit, cname)
        for procedure_row in procedures:
            procedure_row["_request"] = len(request_rows)
            request_rows.append(_served_request(patient, visit, "PROCEDURE", procedure_row["description"]))
            proc_rows.append(procedure_row)

        note_rows.append(
            {
                "visit_id": visit.id,
                "note_type": NoteType.SOAP,
                "text": render_soap(
                    provider_name=provider_names.get(visit.provider_id, "Unassigned"),
                    visit_date=visit.visit_date,
                    chief_complaint=visit.chief_complaint,
                    follow_up_days=cprofile.follow_up_days,
                    age=patient.age,
                    sex=sex_word,
                    allergies=list(facts.allergies),
                    chronic_history=[c.description for c in chronic_seen.values()],
                    family_history=list(facts.family_lines),
                    vital=vital,
                    conditions=[(cprofile.description, cprofile.icd10_code)],
                    prescriptions=visit_rx,
                    referrals=visit_referrals,
                    advice=visit_advice,
                    labs=[
                        (lab.test_name, lab.value, lab.unit, str(lab.status).split(".")[-1])
                        for lab in visit_labs
                    ],
                    procedures=[row["description"] for row in procedures],
                ),
                "author_id": visit.provider_id,
            }
        )

    _place_requests(
        session,
        request_rows,
        (lab_rows, proc_rows, rx_rows, dispense_rows),
        follow_ups,
        [v for v, _c, _n in visit_tuples],
    )

    for model, rows in (
        (Vital, vital_rows),
        (Prescription, rx_rows),
        (LabResult, lab_rows),
        (Procedure, proc_rows),
        (MedicationDispense, dispense_rows),
        (VisitNote, note_rows),
    ):
        if rows:
            session.execute(sa_insert(model), rows)

    # staged conditions: the problem-list row reflects the FINAL severity
    # stage the trajectory reached (design clinical-breadth.md §5 amendment)
    for name, stage in history.final_stages.items():
        row = chronic_seen.get(name)
        if row is not None and row.icd10_code != stage.icd10_code:
            row.icd10_code = stage.icd10_code
            row.description = stage.description

    generate_medication_statements(session, patient, chronic_seen, rx_stream)
    generate_immunizations(session, patient, set(chronic_seen), as_of=scope.as_of)
    return len(visit_tuples), final_chronic


def build_dataset(  # quality: allow(no-god-class) — keyword-only knobs ARE the public generation API
    session,
    n_patients: int = 10_000,
    years_of_history: int = 4,
    verbose: bool = True,
    *,
    catalog: ConditionCatalog | None = None,
    seed: int | None = None,
    progression_cadence: str = "yearly",
    as_of: date | None = None,
):
    """Generate n_patients patients (as households) with full charts and commit.

    ``catalog`` injects the condition set (tests pass small ones); None
    means the default assembly of core packs. ``seed`` makes the whole
    run reproducible (same seed, same dataset). ``progression_cadence``
    ('yearly' | 'quarterly') sets how often staged chronic conditions
    re-evaluate severity (design clinical-breadth.md §5 amendment)."""
    from sqlalchemy import text as sa_text

    if progression_cadence not in ("yearly", "quarterly"):
        raise ValueError(f"progression_cadence must be 'yearly' or 'quarterly', not {progression_cadence!r}")
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    CHUNK = 500
    total_visits = 0
    generated = 0

    # Bulk-generation session settings: no attribute expiry on chunk commits
    # (re-loading every patient attribute cost thousands of queries), and
    # relaxed SQLite durability during the build (single-writer, disposable)
    session.expire_on_commit = False
    if session.get_bind().dialect.name == "sqlite":
        session.execute(sa_text("PRAGMA journal_mode=WAL"))
        session.execute(sa_text("PRAGMA synchronous=OFF"))

    # Generating into an existing database is legal (appending a panel) —
    # the MRN uniqueness set is RESET to exactly what the database holds,
    # so a prior in-process run can't leak MRNs into this one (which
    # would silently break same-seed reproducibility)
    _issued_mrns.clear()
    _issued_mrns.update(mrn for (mrn,) in session.query(Patient.mrn))

    providers = seed_providers(session)
    scope = RunScope(
        catalog=catalog or default_catalog(),
        providers=tuple(providers),
        years=years_of_history,
        rng=random.Random(seed),
        periods_per_year={"yearly": 1, "quarterly": 4}[progression_cadence],
        as_of=as_of or TODAY,
    )

    for size in _household_sizes(n_patients):
        surname = fake.last_name()
        address = (fake.street_address(), fake.city(), fake.state_abbr(), fake.zipcode())
        ages = _household_ages(size)

        members: list[Patient] = []
        parent_conditions: dict[int, set] = {}
        adults = [a for a in ages if a >= 18]
        children = [a for a in ages if a < 18]

        # adults first — their real conditions drive the children's heredity
        for age in adults:
            patient = generate_patient(age, surname=surname, address=address, as_of=scope.as_of)
            session.add(patient)
            session.flush()
            allergy_names = generate_allergies(session, patient)
            fam_hx, fam_lines = generate_extended_relatives(session, patient, age)
            n_visits, chronic = _generate_one(
                session,
                patient,
                fam_hx,
                scope,
                NoteFacts(tuple(allergy_names), tuple(fam_lines)),
            )
            parent_conditions[patient.id] = chronic
            total_visits += n_visits
            members.append(patient)

        for age in children:
            patient = generate_patient(age, surname=surname, address=address, as_of=scope.as_of)
            session.add(patient)
            session.flush()
            allergy_names = generate_allergies(session, patient)
            fam_hx = hereditary_from_patients(members, parent_conditions, scope.catalog)
            fam_lines = record_parent_history(
                session, patient, [m for m in members if m.age >= 18], parent_conditions, scope.catalog
            )
            n_visits, _chronic = _generate_one(
                session,
                patient,
                fam_hx,
                scope,
                NoteFacts(tuple(allergy_names), tuple(fam_lines)),
            )
            total_visits += n_visits
            members.append(patient)

        link_household(session, members)
        generated += len(members)

        if generated // CHUNK != (generated - len(members)) // CHUNK:
            session.commit()
            if verbose:
                print(f"  ✓ {generated:,} patients committed  ({total_visits:,} visits so far)")

    session.commit()
    if verbose:
        print(f"\n✅ Done — {generated:,} patients, {total_visits:,} visits generated.")
    return generated, total_visits
