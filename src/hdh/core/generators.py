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
from datetime import date, timedelta

from faker import Faker
from sqlalchemy import insert as sa_insert

from .disease_engine import (
    CONDITIONS,
    LabSpec,
    comorbidity_seeds,
    pick_condition,
)
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
    MedicationStatement,
    MedicationStatus,
    NoteType,
    Patient,
    Prescription,
    Procedure,
    Provider,
    Specialty,
    Visit,
    VisitNote,
    Vital,
)

fake = Faker("en_US")
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

CHRONIC_NAMES = frozenset(
    {
        "hypertension",
        "type2_diabetes",
        "hyperlipidemia",
        "copd",
        "osteoarthritis",
        "hypothyroidism",
        "obesity",
    }
)

# condition-name → hereditary-risk key consumed by comorbidity_seeds
HEREDITARY_KEYS = {"type2_diabetes": "diabetes", "hypertension": "hypertension"}

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


def _primary_provider(providers: list[Provider], age: int) -> Provider:
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
) -> Patient:
    """Generate one synthetic patient of a given age (household-aware)."""
    sex = random.choice(["M", "F"])
    dob = date.today() - timedelta(days=age * 365 + random.randint(0, 364))
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


def hereditary_from_patients(parents: list[Patient], parent_conditions: dict[int, set]) -> dict[str, bool]:
    """Derive a child's hereditary-risk flags from parents' ACTUAL conditions."""
    flags: dict[str, bool] = {}
    for parent in parents:
        for cname in parent_conditions.get(parent.id, set()):
            key = HEREDITARY_KEYS.get(cname)
            if key:
                flags[key] = True
    return flags


def record_parent_history(
    session, child: Patient, parents: list[Patient], parent_conditions: dict[int, set]
) -> list[str]:
    """FamilyHistory rows on the child for each parent's chronic conditions;
    returns display lines for note rendering."""
    lines: list[str] = []
    for parent in parents:
        rel = "mother" if str(parent.sex).endswith("F") else "father"
        for cname in parent_conditions.get(parent.id, set()):
            profile = CONDITIONS.get(cname)
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


def generate_visit_history(patient: Patient, fam_hx: dict, smoker: bool, years: int = 4) -> tuple[list, set]:
    """
    Generate a realistic multi-year visit history for a patient.
    Returns (list of (Visit, profile, condition-name) tuples, final chronic set).
    """
    age_at_start = max(0, patient.age - years)
    established = comorbidity_seeds(patient.age, fam_hx, smoker, _bmi(patient))

    start_date = date.today() - timedelta(days=years * 365)
    all_visits = []

    visits_per_yr = _visits_per_year(patient.age, established)
    total_visits = max(1, int(random.gauss(visits_per_yr * years, years * 0.5)))
    total_visits = _clamp(total_visits, 1, 80)

    visit_dates = sorted(
        random.sample(
            [start_date + timedelta(days=d) for d in range(years * 365)], k=min(total_visits, years * 365)
        )
    )

    for vdate in visit_dates:
        visit_age = age_at_start + (vdate - start_date).days // 365

        eligible_established = established.copy()
        cprofile, cname = pick_condition(visit_age, vdate.month, eligible_established)
        if patient.sex == "M" and cname in ("uti", "contraception_consult"):
            cprofile, cname = pick_condition(visit_age, vdate.month, eligible_established)

        visit = Visit(
            patient_id=patient.id,
            visit_date=vdate,
            visit_type=cprofile.visit_type,
            chief_complaint=cprofile.chief_complaint,
            follow_up_days=cprofile.follow_up_days,
        )
        all_visits.append((visit, cprofile, cname))

        if cname in CHRONIC_NAMES:
            established.add(cname)

    return all_visits, established


def _emit_conditions(
    session, patient: Patient, visit: Visit, cprofile, cname: str, chronic_seen: dict
) -> None:
    """Unified problem list: acute visits get resolved encounter conditions;
    chronic conditions get ONE active row at first diagnosis."""
    if cname in CHRONIC_NAMES:
        if cname not in chronic_seen:
            cond = Condition(
                patient_id=patient.id,
                visit_id=visit.id,
                icd10_code=cprofile.icd10_code,
                description=cprofile.description,
                chronic=True,
                status=ConditionStatus.ACTIVE,
                controlled=random.random() > 0.25,
                onset_date=visit.visit_date,
            )
            session.add(cond)
            session.flush()
            chronic_seen[cname] = cond
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


def generate_immunizations(session, patient: Patient, chronic: set) -> None:
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
                season = date.today().year - years_back
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
                "administered_date": date.today() - timedelta(days=random.randint(0, 3650)),
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


def _household_ages(size: int) -> list[int]:
    """Coherent ages: adults first, children 20–40 years younger."""
    if size == 1:
        return [random.choice([random.randint(18, 65), random.randint(66, 90)])]
    parent_age = random.randint(28, 62)
    ages = [parent_age]
    if size >= 2 and random.random() < 0.75:
        ages.append(_clamp(parent_age + random.randint(-6, 6), 20, 90))
    while len(ages) < size:
        child_age = parent_age - random.randint(20, 40)
        ages.append(int(_clamp(child_age, 0, 17)))
    return ages[:size]


def _row(obj, model) -> dict:
    """Extract a bulk-insert dict from a transient ORM object (skip null pk)."""
    return {
        c.name: getattr(obj, c.name)
        for c in model.__table__.columns
        if not (c.primary_key and getattr(obj, c.name) is None)
    }


def _generate_one(
    session,
    patient: Patient,
    fam_hx: dict,
    providers,
    years: int,
    allergies: list[str] | None = None,
    family_lines: list[str] | None = None,
) -> tuple[int, set]:
    """Visits, conditions, notes, and per-patient chart entities.

    The hot path: ONE flush for all visits, notes rendered from the
    in-memory objects (zero lazy loads), children bulk-inserted.
    Returns (visit count, final chronic set)."""
    from .notes import render_soap

    primary = _primary_provider(providers, patient.age)
    visit_tuples, final_chronic = generate_visit_history(patient, fam_hx, bool(patient.smoker), years)

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
    rx_stream: list[tuple] = []
    sex_word = "male" if str(patient.sex).endswith("M") else "female"

    for visit, cprofile, cname in visit_tuples:
        vital = generate_vital(visit.id, patient.age, patient.sex, _bmi(patient), cprofile)
        vital_rows.append(_row(vital, Vital))

        _emit_conditions(session, patient, visit, cprofile, cname, chronic_seen)

        visit_rx: list[dict] = []
        if cprofile.rx_options:
            if cprofile.rx_pick_all:
                rx_list = cprofile.rx_options
            else:
                n_rx = random.choices([1, 2], weights=[75, 25], k=1)[0]
                rx_list = random.sample(cprofile.rx_options, k=min(n_rx, len(cprofile.rx_options)))
            for rx_spec in rx_list:
                rx = {
                    "visit_id": visit.id,
                    "drug_name": rx_spec.drug_name,
                    "drug_class": rx_spec.drug_class,
                    "dose": rx_spec.dose,
                    "frequency": rx_spec.frequency,
                    "duration_days": rx_spec.duration_days,
                    "refills": rx_spec.refills,
                    "is_new": cname not in final_chronic or random.random() > 0.5,
                }
                rx_rows.append(rx)
                visit_rx.append(rx)
                rx_stream.append((visit.visit_date, rx))

        visit_labs = [generate_lab(visit.id, spec, has_condition=True) for spec in cprofile.labs]
        lab_rows.extend(_row(lab, LabResult) for lab in visit_labs)

        procedures = _procedures_for(patient, visit, cname)
        proc_rows.extend(procedures)

        note_rows.append(
            {
                "visit_id": visit.id,
                "note_type": NoteType.SOAP,
                "text": render_soap(
                    provider_name=provider_names.get(visit.provider_id, "Unassigned"),
                    visit_date=visit.visit_date,
                    chief_complaint=visit.chief_complaint,
                    follow_up_days=visit.follow_up_days,
                    age=patient.age,
                    sex=sex_word,
                    allergies=allergies or [],
                    chronic_history=[c.description for c in chronic_seen.values()],
                    family_history=family_lines or [],
                    vital=vital,
                    conditions=[(cprofile.description, cprofile.icd10_code)],
                    prescriptions=visit_rx,
                    labs=[
                        (lab.test_name, lab.value, lab.unit, str(lab.status).split(".")[-1])
                        for lab in visit_labs
                    ],
                    procedures=[row["description"] for row in procedures],
                ),
                "author_id": visit.provider_id,
            }
        )

    for table, rows in (
        (Vital.__table__, vital_rows),
        (Prescription.__table__, rx_rows),
        (LabResult.__table__, lab_rows),
        (Procedure.__table__, proc_rows),
        (VisitNote.__table__, note_rows),
    ):
        if rows:
            session.execute(sa_insert(table), rows)

    generate_medication_statements(session, patient, chronic_seen, rx_stream)
    generate_immunizations(session, patient, set(chronic_seen))
    return len(visit_tuples), final_chronic


def build_dataset(session, n_patients: int = 10_000, years_of_history: int = 4, verbose: bool = True):
    """
    Generate n_patients patients (as households) with full charts and commit.
    """
    from sqlalchemy import text as sa_text

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
    # seed the MRN uniqueness set with what's already there
    _issued_mrns.update(mrn for (mrn,) in session.query(Patient.mrn))

    providers = seed_providers(session)

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
            patient = generate_patient(age, surname=surname, address=address)
            session.add(patient)
            session.flush()
            allergy_names = generate_allergies(session, patient)
            fam_hx, fam_lines = generate_extended_relatives(session, patient, age)
            n_visits, chronic = _generate_one(
                session,
                patient,
                fam_hx,
                providers,
                years_of_history,
                allergies=allergy_names,
                family_lines=fam_lines,
            )
            parent_conditions[patient.id] = chronic
            total_visits += n_visits
            members.append(patient)

        for age in children:
            patient = generate_patient(age, surname=surname, address=address)
            session.add(patient)
            session.flush()
            allergy_names = generate_allergies(session, patient)
            fam_hx = hereditary_from_patients(members, parent_conditions)
            fam_lines = record_parent_history(
                session, patient, [m for m in members if m.age >= 18], parent_conditions
            )
            n_visits, _chronic = _generate_one(
                session,
                patient,
                fam_hx,
                providers,
                years_of_history,
                allergies=allergy_names,
                family_lines=fam_lines,
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
