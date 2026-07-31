"""
Generators for synthetic Family Medicine patients and their visit histories.
"""

import random
import string
from datetime import date, timedelta
from faker import Faker

from .models import Patient, Visit, Vital, Diagnosis, Prescription, LabResult, ChronicCondition
from .disease_engine import (
    CONDITIONS, pick_condition, comorbidity_seeds,
    LabSpec,
)
from .models import LabStatus

fake = Faker("en_US")
Faker.seed(42)
random.seed(42)


# ─── Helpers ──────────────────────────────────────────────────────────────────

INSURERS = [
    "Blue Cross Blue Shield", "Aetna", "UnitedHealthcare", "Cigna",
    "Humana", "Medicare", "Medicaid", "Kaiser Permanente",
    "Anthem", "Molina Healthcare",
]

BLOOD_TYPES = ["A+","A-","B+","B-","AB+","AB-","O+","O-"]

ALLERGENS = [
    "Penicillin", "Sulfa drugs", "Aspirin", "Ibuprofen", "Codeine",
    "Latex", "Shellfish", "Peanuts", "Tree nuts", "Bee stings",
    "Amoxicillin", "Erythromycin", "Ciprofloxacin",
]

PROVIDERS = [
    "Dr. Sarah Mitchell, MD", "Dr. James O'Brien, MD",
    "Dr. Priya Sharma, MD",   "Dr. Robert Chen, MD",
    "Dr. Angela Torres, DO",  "Dr. Michael Park, MD",
]

RACES = [
    "White", "Black or African American", "Asian",
    "American Indian or Alaska Native", "Pacific Islander", "Other",
]

ETHNICITIES = ["Non-Hispanic or Latino", "Hispanic or Latino"]


def _random_mrn() -> str:
    return "MRN" + "".join(random.choices(string.digits, k=8))


def _weighted_age() -> int:
    """Return an age sampled from a realistic family medicine age distribution."""
    # Rough distribution: children 20%, teens 8%, adults 45%, seniors 27%
    bucket = random.choices(
        ["child", "teen", "adult", "senior"],
        weights=[20, 8, 45, 27], k=1
    )[0]
    if bucket == "child":  return random.randint(0, 12)
    if bucket == "teen":   return random.randint(13, 17)
    if bucket == "adult":  return random.randint(18, 65)
    return random.randint(66, 90)


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


# ─── Patient Generator ────────────────────────────────────────────────────────

def generate_patient() -> Patient:
    age  = _weighted_age()
    sex  = random.choice(["M", "F"])
    dob  = date.today() - timedelta(days=age * 365 + random.randint(0, 364))
    bmi  = _baseline_bmi(age)

    # Family history (more likely in older patients)
    fam_htn  = random.random() < (0.4 if age > 40 else 0.25)
    fam_dm   = random.random() < (0.35 if age > 40 else 0.20)
    fam_hd   = random.random() < 0.25
    fam_ca   = random.random() < 0.20

    # Smoker (15% adults)
    smoker   = age >= 18 and random.random() < 0.15

    # Allergies
    n_allergies = random.choices([0, 1, 2], weights=[60, 30, 10], k=1)[0]
    allergies   = "|".join(random.sample(ALLERGENS, n_allergies)) if n_allergies else "NKDA"

    # Name — use child-appropriate or adult names
    if sex == "M":
        fname = fake.first_name_male()
    else:
        fname = fake.first_name_female()

    p = Patient(
        mrn             = _random_mrn(),
        first_name      = fname,
        last_name       = fake.last_name(),
        date_of_birth   = dob,
        sex             = sex,
        race            = random.choice(RACES),
        ethnicity       = random.choices(ETHNICITIES, weights=[80, 20], k=1)[0],
        address         = fake.street_address(),
        city            = fake.city(),
        state           = fake.state_abbr(),
        zip_code        = fake.zipcode(),
        phone           = fake.phone_number()[:15],
        email           = fake.email() if age >= 14 else "",
        insurance_name  = random.choice(INSURERS),
        insurance_id    = fake.bothify("???-########"),
        blood_type      = random.choice(BLOOD_TYPES),
        allergies       = allergies,
        fam_hx_diabetes     = fam_dm,
        fam_hx_hypertension = fam_htn,
        fam_hx_heart_disease= fam_hd,
        fam_hx_cancer       = fam_ca,
        smoker          = smoker,
        bmi_baseline    = bmi,
    )
    return p, {"diabetes": fam_dm, "hypertension": fam_htn}, smoker


# ─── Vital Generator ──────────────────────────────────────────────────────────

def _baseline_vitals(age: int, sex: str, bmi: float):
    """Return baseline (healthy) vitals for this patient."""
    # BP rises with age
    sys_base = 110 + min(age // 4, 20) + (5 if sex == "M" else 0)
    dia_base = 70 + min(age // 6, 12)
    hr_base  = 72
    rr_base  = 16
    temp_base = 98.6
    spo2_base = 98
    return sys_base, dia_base, hr_base, rr_base, temp_base, spo2_base


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def generate_vital(visit_id: int, age: int, sex: str, bmi: float, condition_profile) -> Vital:
    cp = condition_profile
    sys_b, dia_b, hr_b, rr_b, temp_b, spo2_b = _baseline_vitals(age, sex, bmi)

    bp_sys = int(_clamp(random.gauss(sys_b + cp.bp_sys_delta[0], cp.bp_sys_delta[1]), 85, 220))
    bp_dia = int(_clamp(random.gauss(dia_b + cp.bp_dia_delta[0], cp.bp_dia_delta[1]), 50, 130))
    hr     = int(_clamp(random.gauss(hr_b  + cp.hr_delta[0],    cp.hr_delta[1]),    45, 160))
    rr     = int(_clamp(random.gauss(rr_b  + cp.rr_delta[0],    cp.rr_delta[1]),    10, 40))
    temp   = round(_clamp(random.gauss(temp_b + cp.temp_delta[0], cp.temp_delta[1]), 96.0, 105.0), 1)
    spo2   = int(_clamp(random.gauss(spo2_b + cp.spo2_delta[0], cp.spo2_delta[1]),  80, 100))

    # BMI fluctuates slightly visit to visit
    bmi_v  = round(max(12.0, random.gauss(bmi, 0.8)), 1)
    ht_cm  = _height_cm(age, sex)
    wt_kg  = round(bmi_v * (ht_cm / 100) ** 2, 1)
    pain   = int(_clamp(round(random.gauss(cp.pain[0], cp.pain[1])), 0, 10))

    return Vital(
        visit_id        = visit_id,
        bp_systolic     = bp_sys,
        bp_diastolic    = bp_dia,
        heart_rate      = hr,
        respiratory_rate= rr,
        temperature_f   = temp,
        oxygen_sat      = spo2,
        weight_kg       = wt_kg,
        height_cm       = ht_cm,
        bmi             = bmi_v,
        pain_scale      = pain,
    )


# ─── Lab Generator ────────────────────────────────────────────────────────────

def generate_lab(visit_id: int, spec: LabSpec, has_condition: bool = True) -> LabResult:
    if has_condition and spec.condition_shift != 0:
        val = random.gauss(
            spec.normal_mean + spec.condition_shift,
            spec.condition_shift_sd or spec.normal_sd
        )
    else:
        val = random.gauss(spec.normal_mean, spec.normal_sd)

    val = round(val, 2)

    if val < spec.ref_low:
        status = LabStatus.LOW
    elif val > spec.ref_high:
        status = LabStatus.HIGH
    else:
        status = LabStatus.NORMAL
    # Critical thresholds
    if spec.test_name == "Glucose (stat)" and val > 400:
        status = LabStatus.CRITICAL
    if spec.test_name == "WBC" and val > 20:
        status = LabStatus.CRITICAL

    return LabResult(
        visit_id       = visit_id,
        test_name      = spec.test_name,
        value          = val,
        unit           = spec.unit,
        reference_low  = spec.ref_low,
        reference_high = spec.ref_high,
        status         = status,
        loinc_code     = spec.loinc_code,
    )


# ─── Visit Generator ──────────────────────────────────────────────────────────

def _visits_per_year(age: int, established_conditions: set) -> float:
    """Estimate average visits/year for this patient profile."""
    base = 2.0
    if age < 3:    base = 5.0
    elif age < 18: base = 2.5
    elif age > 65: base = 5.0
    elif age > 45: base = 3.5
    # Each chronic condition adds ~1 visit/year
    base += len(established_conditions) * 0.9
    return base


def generate_visit_history(patient: Patient, fam_hx: dict, smoker: bool,
                            years: int = 4) -> list[Visit]:
    """
    Generate a realistic multi-year visit history for a patient.
    Returns a list of Visit objects (with vitals, diagnoses, Rx, labs attached).
    """
    age_at_start = max(0, patient.age - years)
    established  = comorbidity_seeds(patient.age, fam_hx, smoker, patient.bmi_baseline)

    start_date = date.today() - timedelta(days=years * 365)
    all_visits = []

    visits_per_yr = _visits_per_year(patient.age, established)
    total_visits  = max(1, int(random.gauss(visits_per_yr * years, years * 0.5)))
    total_visits  = _clamp(total_visits, 1, 80)

    # Spread visits across the date range with slight clustering
    visit_dates = sorted(
        random.sample(
            [start_date + timedelta(days=d) for d in range(years * 365)],
            k=min(total_visits, years * 365)
        )
    )

    for vdate in visit_dates:
        visit_age = age_at_start + (vdate - start_date).days // 365

        # Female-only conditions
        eligible_established = established.copy()
        if patient.sex == "F":
            eligible_established.discard("contraception_consult")  # keep it eligible

        cprofile, cname = pick_condition(visit_age, vdate.month, eligible_established)

        # Skip female-only for males
        if patient.sex == "M" and cname in ("uti", "contraception_consult"):
            # Re-pick
            cprofile, cname = pick_condition(visit_age, vdate.month, eligible_established)

        # Build the visit (id assigned by DB, use placeholder)
        visit = Visit(
            patient_id      = patient.id,
            visit_date      = vdate,
            visit_type      = cprofile.visit_type,
            chief_complaint = cprofile.chief_complaint,
            provider_name   = random.choice(PROVIDERS),
            follow_up_days  = cprofile.follow_up_days,
        )
        all_visits.append((visit, cprofile, cname))

        # Track new chronic diagnoses
        if cname in ("hypertension","type2_diabetes","hyperlipidemia",
                     "copd","osteoarthritis","hypothyroidism","obesity"):
            established.add(cname)

    return all_visits, established


# ─── Full Dataset Builder ─────────────────────────────────────────────────────

def build_dataset(session, n_patients: int = 10_000,
                  years_of_history: int = 4,
                  verbose: bool = True):
    """
    Generate n_patients patients with full visit histories and commit to DB.
    """
    from .models import ChronicCondition

    CHUNK = 500
    total_visits = 0

    for i in range(n_patients):
        patient, fam_hx, smoker = generate_patient()
        session.add(patient)
        session.flush()   # get patient.id

        visit_tuples, final_conditions = generate_visit_history(
            patient, fam_hx, smoker, years=years_of_history
        )

        for visit, cprofile, cname in visit_tuples:
            visit.patient_id = patient.id
            session.add(visit)
            session.flush()   # get visit.id

            # Vitals
            vital = generate_vital(visit.id, patient.age,
                                   patient.sex, patient.bmi_baseline, cprofile)
            session.add(vital)

            # Primary diagnosis
            dx = Diagnosis(
                visit_id    = visit.id,
                icd10_code  = cprofile.icd10_code,
                description = cprofile.description,
                is_primary  = True,
            )
            session.add(dx)

            # Prescriptions
            if cprofile.rx_options:
                if cprofile.rx_pick_all:
                    rx_list = cprofile.rx_options
                else:
                    n_rx = random.choices([1, 2], weights=[75, 25], k=1)[0]
                    rx_list = random.sample(cprofile.rx_options,
                                            k=min(n_rx, len(cprofile.rx_options)))
                for rx_spec in rx_list:
                    is_new = cname not in final_conditions or random.random() > 0.5
                    rx = Prescription(
                        visit_id    = visit.id,
                        drug_name   = rx_spec.drug_name,
                        drug_class  = rx_spec.drug_class,
                        dose        = rx_spec.dose,
                        frequency   = rx_spec.frequency,
                        duration_days = rx_spec.duration_days,
                        refills     = rx_spec.refills,
                        is_new      = is_new,
                    )
                    session.add(rx)

            # Labs
            for lab_spec in cprofile.labs:
                lr = generate_lab(visit.id, lab_spec, has_condition=True)
                session.add(lr)

            total_visits += 1

        # Add chronic conditions summary to patient record
        for cname in final_conditions:
            cond = CONDITIONS.get(cname)
            if cond:
                cc = ChronicCondition(
                    patient_id  = patient.id,
                    icd10_code  = cond.icd10_code,
                    description = cond.description,
                    onset_date  = date.today() - timedelta(
                        days=random.randint(180, years_of_history * 365)
                    ),
                    controlled  = random.random() > 0.25,
                )
                session.add(cc)

        # Commit in chunks
        if (i + 1) % CHUNK == 0:
            session.commit()
            if verbose:
                print(f"  ✓ {i+1:,} patients committed  ({total_visits:,} visits so far)")

    session.commit()
    if verbose:
        print(f"\n✅ Done — {n_patients:,} patients, {total_visits:,} visits generated.")
    return n_patients, total_visits
