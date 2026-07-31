"""
Disease Probability Engine for Family Medicine synthetic dataset.

Encodes realistic age/sex/season-weighted disease distributions,
comorbidity clustering, vitals ranges, lab panels, and prescriptions
for each condition — as seen in a typical family physician's OPD.
"""

import random
from dataclasses import dataclass, field

# ─── Data Structures ──────────────────────────────────────────────────────────


@dataclass
class LabSpec:
    test_name: str
    loinc_code: str
    unit: str
    ref_low: float
    ref_high: float
    normal_mean: float
    normal_sd: float
    # Condition-specific shift (positive = elevated, negative = low)
    condition_shift: float = 0.0
    condition_shift_sd: float = 0.0


@dataclass
class RxSpec:
    drug_name: str
    drug_class: str
    dose: str
    frequency: str
    duration_days: int | None  # None = chronic / ongoing
    refills: int = 0


@dataclass
class ConditionProfile:
    icd10_code: str
    description: str
    chief_complaint: str
    visit_type: str  # acute / follow_up / preventive / urgent

    # Vitals modifiers (deltas from patient's baseline)
    bp_sys_delta: tuple = (0, 5)  # (mean delta, sd)
    bp_dia_delta: tuple = (0, 3)
    hr_delta: tuple = (0, 5)
    rr_delta: tuple = (0, 2)
    temp_delta: tuple = (0.0, 0.2)
    spo2_delta: tuple = (0, 1)
    pain: tuple = (0, 1)  # pain scale 0-10

    # Labs ordered for this condition (list of LabSpec)
    labs: list = field(default_factory=list)
    # Prescriptions for this condition (pick 1 randomly from list, or all)
    rx_options: list = field(default_factory=list)
    rx_pick_all: bool = False
    # Typical follow-up in days (None = PRN)
    follow_up_days: int | None = None
    # Seasonal multiplier by month index 1-12 (1.0 = no change)
    seasonal_weights: dict = field(default_factory=lambda: {m: 1.0 for m in range(1, 13)})


# ─── Lab Panel Definitions ────────────────────────────────────────────────────


def cbc():
    return [
        LabSpec("WBC", "6690-2", "K/uL", 4.5, 11.0, 7.0, 1.5),
        LabSpec("Hemoglobin", "718-7", "g/dL", 13.5, 17.5, 15.0, 1.2),
        LabSpec("Platelets", "777-3", "K/uL", 150, 400, 250, 50),
    ]


def cmp():
    return [
        LabSpec("Glucose", "2345-7", "mg/dL", 70, 100, 88, 8),
        LabSpec("Sodium", "2951-2", "mEq/L", 136, 145, 140, 2),
        LabSpec("Potassium", "2823-3", "mEq/L", 3.5, 5.0, 4.1, 0.3),
        LabSpec("Creatinine", "2160-0", "mg/dL", 0.6, 1.2, 0.9, 0.15),
        LabSpec("ALT", "1742-6", "U/L", 7, 56, 25, 10),
    ]


def lipid_panel():
    return [
        LabSpec("Total Cholesterol", "2093-3", "mg/dL", 0, 200, 185, 30),
        LabSpec("LDL", "2089-1", "mg/dL", 0, 100, 110, 25, condition_shift=20, condition_shift_sd=15),
        LabSpec("HDL", "2085-9", "mg/dL", 40, 60, 50, 10),
        LabSpec("Triglycerides", "2571-8", "mg/dL", 0, 150, 140, 30),
    ]


def hba1c():
    return [LabSpec("HbA1c", "4548-4", "%", 4.0, 5.6, 5.1, 0.3, condition_shift=2.8, condition_shift_sd=0.8)]


def tsh():
    return [LabSpec("TSH", "3016-3", "mIU/L", 0.4, 4.0, 2.0, 0.8)]


def ua():
    return [LabSpec("UA WBC", "5767-9", "cells/hpf", 0, 5, 1, 1, condition_shift=30, condition_shift_sd=15)]


def flu_swab():
    return [LabSpec("Influenza A/B", "92142-1", "result", 0, 0, 0, 0)]


def strep_swab():
    return [LabSpec("Rapid Strep", "60489-2", "result", 0, 0, 0, 0)]


def glucose_stat():
    return [
        LabSpec(
            "Glucose (stat)", "2345-7", "mg/dL", 70, 100, 95, 10, condition_shift=55, condition_shift_sd=30
        )
    ]


def bmp():
    return cmp()[:4]  # subset


# ─── Prescription Formularies ─────────────────────────────────────────────────

# Antibiotics for otitis media
OTITIS_RX = [
    RxSpec("Amoxicillin", "Penicillin antibiotic", "250mg", "TID", 10, 0),
    RxSpec("Amoxicillin-Clavulanate", "Penicillin combo", "400mg", "BID", 10, 0),
]

STREP_RX = [
    RxSpec("Amoxicillin", "Penicillin antibiotic", "500mg", "BID", 10, 0),
    RxSpec("Azithromycin", "Macrolide antibiotic", "500mg", "QD", 5, 0),
]

URI_RX = [
    RxSpec("Guaifenesin", "Expectorant", "400mg", "Q4H PRN", 7, 0),
    RxSpec("Dextromethorphan", "Cough suppressant", "30mg", "Q6H PRN", 7, 0),
    RxSpec("Saline nasal spray", "OTC", "2 sprays", "BID", 14, 0),
]

FLU_RX = [
    RxSpec("Oseltamivir (Tamiflu)", "Antiviral", "75mg", "BID", 5, 0),
    RxSpec("Rest & fluids", "Supportive", "—", "—", 7, 0),
]

UTI_RX = [
    RxSpec("Nitrofurantoin", "Antibiotic", "100mg", "BID", 7, 0),
    RxSpec("Trimethoprim-SMX", "Antibiotic", "DS tab", "BID", 3, 0),
]

HTN_RX = [
    RxSpec("Lisinopril", "ACE inhibitor", "10mg", "QD", None, 3),
    RxSpec("Amlodipine", "Calcium channel blocker", "5mg", "QD", None, 3),
    RxSpec("Losartan", "ARB", "50mg", "QD", None, 3),
    RxSpec("Hydrochlorothiazide", "Thiazide diuretic", "25mg", "QD", None, 3),
]

DM_RX = [
    RxSpec("Metformin", "Biguanide", "500mg", "BID", None, 3),
    RxSpec("Metformin", "Biguanide", "1000mg", "BID", None, 3),
    RxSpec("Glipizide", "Sulfonylurea", "5mg", "QD", None, 3),
    RxSpec("Sitagliptin", "DPP-4 inhibitor", "100mg", "QD", None, 3),
]

LIPID_RX = [
    RxSpec("Atorvastatin", "Statin", "20mg", "QD", None, 3),
    RxSpec("Simvastatin", "Statin", "40mg", "QD", None, 3),
    RxSpec("Rosuvastatin", "Statin", "10mg", "QD", None, 3),
]

GERD_RX = [
    RxSpec("Omeprazole", "PPI", "20mg", "QD", 28, 2),
    RxSpec("Pantoprazole", "PPI", "40mg", "QD", 28, 2),
    RxSpec("Famotidine", "H2 blocker", "20mg", "BID", 28, 2),
]

ANXIETY_RX = [
    RxSpec("Sertraline", "SSRI", "50mg", "QD", None, 3),
    RxSpec("Escitalopram", "SSRI", "10mg", "QD", None, 3),
    RxSpec("Buspirone", "Anxiolytic", "10mg", "BID", None, 3),
]

PAIN_RX = [
    RxSpec("Ibuprofen", "NSAID", "600mg", "TID", 7, 0),
    RxSpec("Naproxen", "NSAID", "500mg", "BID", 10, 0),
    RxSpec("Acetaminophen", "Analgesic", "500mg", "Q6H PRN", 7, 0),
]

COPD_RX = [
    RxSpec("Tiotropium (Spiriva)", "LAMA inhaler", "18mcg", "QD", None, 3),
    RxSpec("Salmeterol", "LABA inhaler", "50mcg", "BID", None, 3),
    RxSpec("Albuterol inhaler", "SABA rescue", "2 puffs", "Q4H PRN", None, 3),
]

ACNE_RX = [
    RxSpec("Doxycycline", "Tetracycline antibiotic", "100mg", "QD", 90, 2),
    RxSpec("Benzoyl peroxide 5%", "Topical OTC", "apply", "QD", 90, 2),
]

EAR_DROPS = [
    RxSpec("Antipyrine-Benzocaine", "Otic analgesic drops", "4 drops", "TID PRN", 7, 0),
]

LACERATION_RX = [
    RxSpec("Cephalexin", "Antibiotic (prophylaxis)", "500mg", "QID", 7, 0),
]


# ─── Flu seasonal pattern ─────────────────────────────────────────────────────
FLU_SEASON = {
    1: 2.5,
    2: 2.0,
    3: 1.2,
    4: 0.5,
    5: 0.3,
    6: 0.2,
    7: 0.2,
    8: 0.2,
    9: 0.5,
    10: 1.0,
    11: 1.8,
    12: 2.5,
}
RSV_SEASON = {
    1: 2.0,
    2: 1.5,
    3: 0.8,
    4: 0.4,
    5: 0.2,
    6: 0.1,
    7: 0.1,
    8: 0.1,
    9: 0.5,
    10: 1.2,
    11: 2.0,
    12: 2.5,
}
FLAT = {m: 1.0 for m in range(1, 13)}
SUMMER_PEAK = {m: (1.5 if m in (6, 7, 8) else 0.7) for m in range(1, 13)}


# ─── Condition Library ────────────────────────────────────────────────────────

CONDITIONS: dict[str, ConditionProfile] = {
    # ── Pediatric (0-12) ──────────────────────────────────────────────────────
    "otitis_media": ConditionProfile(
        icd10_code="H66.90",
        description="Otitis media, unspecified",
        chief_complaint="Ear pain / pulling at ear",
        visit_type="acute",
        temp_delta=(1.2, 0.6),
        pain=(5, 2),
        labs=[],
        rx_options=OTITIS_RX + EAR_DROPS,
        rx_pick_all=False,
        follow_up_days=14,
        seasonal_weights=FLU_SEASON,
    ),
    "well_child": ConditionProfile(
        icd10_code="Z00.129",
        description="Well-child visit, routine",
        chief_complaint="Well-child check / routine vaccines",
        visit_type="preventive",
        pain=(0, 0),
        labs=[],
        rx_options=[],
        follow_up_days=365,
        seasonal_weights=FLAT,
    ),
    "rsv": ConditionProfile(
        icd10_code="J21.0",
        description="RSV bronchiolitis",
        chief_complaint="Wheezing, runny nose, cough",
        visit_type="acute",
        temp_delta=(1.5, 0.7),
        hr_delta=(15, 5),
        rr_delta=(8, 3),
        spo2_delta=(-4, 2),
        pain=(3, 2),
        labs=cbc(),
        rx_options=[RxSpec("Saline nasal suction", "Supportive", "—", "PRN", 7, 0)],
        follow_up_days=3,
        seasonal_weights=RSV_SEASON,
    ),
    "febrile_illness": ConditionProfile(
        icd10_code="R50.9",
        description="Fever, unspecified",
        chief_complaint="Fever",
        visit_type="acute",
        temp_delta=(2.5, 0.8),
        hr_delta=(15, 5),
        pain=(3, 2),
        labs=cbc(),
        rx_options=[RxSpec("Acetaminophen", "Analgesic/Antipyretic", "15mg/kg", "Q6H PRN", 5, 0)],
        follow_up_days=3,
        seasonal_weights=FLU_SEASON,
    ),
    "strep_throat_ped": ConditionProfile(
        icd10_code="J02.0",
        description="Streptococcal pharyngitis",
        chief_complaint="Sore throat, fever",
        visit_type="acute",
        temp_delta=(1.8, 0.6),
        pain=(5, 2),
        labs=strep_swab() + cbc(),
        rx_options=STREP_RX,
        follow_up_days=None,
        seasonal_weights=FLU_SEASON,
    ),
    "conjunctivitis": ConditionProfile(
        icd10_code="H10.9",
        description="Conjunctivitis, unspecified",
        chief_complaint="Red/pink eye, discharge",
        visit_type="acute",
        pain=(2, 1),
        labs=[],
        rx_options=[
            RxSpec("Erythromycin ophthalmic ointment", "Antibiotic eye ointment", "apply", "TID", 7, 0),
            RxSpec("Olopatadine eye drops", "Antihistamine", "1 drop", "BID", 14, 0),
        ],
        follow_up_days=None,
        seasonal_weights=FLAT,
    ),
    "rash_eczema": ConditionProfile(
        icd10_code="L20.9",
        description="Atopic dermatitis / eczema",
        chief_complaint="Skin rash / itching",
        visit_type="acute",
        pain=(2, 1),
        labs=[],
        rx_options=[
            RxSpec("Hydrocortisone 1% cream", "Topical steroid", "apply", "BID", 14, 2),
            RxSpec("Triamcinolone cream", "Topical steroid", "apply", "BID", 14, 1),
        ],
        follow_up_days=30,
        seasonal_weights=FLAT,
    ),
    # ── Adolescent (13-17) ────────────────────────────────────────────────────
    "acne": ConditionProfile(
        icd10_code="L70.0",
        description="Acne vulgaris",
        chief_complaint="Facial acne breakout",
        visit_type="acute",
        pain=(1, 1),
        labs=[],
        rx_options=ACNE_RX,
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    "sports_physical": ConditionProfile(
        icd10_code="Z02.5",
        description="Pre-participation sports physical exam",
        chief_complaint="Sports physical / clearance",
        visit_type="preventive",
        pain=(0, 0),
        labs=[],
        rx_options=[],
        follow_up_days=365,
        seasonal_weights={m: (2.0 if m in (7, 8) else 0.8) for m in range(1, 13)},
    ),
    "sports_injury": ConditionProfile(
        icd10_code="S93.401A",
        description="Sprain of ankle, unspecified",
        chief_complaint="Ankle / knee pain after sports",
        visit_type="urgent",
        pain=(6, 2),
        labs=[],
        rx_options=PAIN_RX,
        follow_up_days=7,
        seasonal_weights=SUMMER_PEAK,
    ),
    "mononucleosis": ConditionProfile(
        icd10_code="B27.00",
        description="Infectious mononucleosis",
        chief_complaint="Severe sore throat, fatigue, lymph nodes",
        visit_type="acute",
        temp_delta=(1.5, 0.5),
        hr_delta=(10, 5),
        pain=(6, 2),
        labs=cbc() + [LabSpec("Monospot", "5334-9", "result", 0, 0, 0, 0)],
        rx_options=[RxSpec("Rest & fluids", "Supportive", "—", "—", 14, 0)],
        follow_up_days=14,
        seasonal_weights=FLAT,
    ),
    "anxiety_teen": ConditionProfile(
        icd10_code="F41.1",
        description="Generalized anxiety disorder",
        chief_complaint="Anxiety, stress, difficulty sleeping",
        visit_type="acute",
        hr_delta=(8, 4),
        pain=(2, 1),
        labs=tsh(),
        rx_options=[RxSpec("Behavioral therapy referral", "Referral", "—", "—", None, 0)] + ANXIETY_RX,
        follow_up_days=30,
        seasonal_weights=FLAT,
    ),
    # ── Young Adult (18-35) ───────────────────────────────────────────────────
    "annual_physical_adult": ConditionProfile(
        icd10_code="Z00.00",
        description="Encounter for general adult medical examination",
        chief_complaint="Annual physical exam",
        visit_type="preventive",
        pain=(0, 0),
        labs=cbc() + cmp() + lipid_panel() + tsh(),
        rx_options=[],
        follow_up_days=365,
        seasonal_weights=FLAT,
    ),
    "influenza": ConditionProfile(
        icd10_code="J11.1",
        description="Influenza with other respiratory manifestations",
        chief_complaint="Flu symptoms — fever, body aches, cough",
        visit_type="acute",
        temp_delta=(2.5, 0.7),
        hr_delta=(15, 5),
        pain=(7, 2),
        labs=flu_swab(),
        rx_options=FLU_RX,
        follow_up_days=None,
        seasonal_weights=FLU_SEASON,
    ),
    "uri_adult": ConditionProfile(
        icd10_code="J06.9",
        description="Acute upper respiratory infection, unspecified",
        chief_complaint="Cold symptoms, nasal congestion, sore throat",
        visit_type="acute",
        temp_delta=(0.8, 0.5),
        pain=(3, 2),
        labs=[],
        rx_options=URI_RX,
        follow_up_days=None,
        seasonal_weights=FLU_SEASON,
    ),
    "uti": ConditionProfile(
        icd10_code="N39.0",
        description="Urinary tract infection",
        chief_complaint="Dysuria, frequency, urinary urgency",
        visit_type="acute",
        temp_delta=(0.8, 0.5),
        pain=(4, 2),
        labs=ua(),
        rx_options=UTI_RX,
        follow_up_days=7,
        seasonal_weights=SUMMER_PEAK,
    ),
    "minor_laceration": ConditionProfile(
        icd10_code="S01.81XA",
        description="Open wound, unspecified head",
        chief_complaint="Cut / laceration requiring sutures",
        visit_type="urgent",
        pain=(6, 2),
        labs=[],
        rx_options=LACERATION_RX,
        follow_up_days=7,
        seasonal_weights=SUMMER_PEAK,
    ),
    "low_back_pain": ConditionProfile(
        icd10_code="M54.5",
        description="Low back pain",
        chief_complaint="Lower back pain",
        visit_type="acute",
        pain=(5, 2),
        labs=[],
        rx_options=PAIN_RX + [RxSpec("Cyclobenzaprine", "Muscle relaxant", "10mg", "TID PRN", 7, 0)],
        follow_up_days=14,
        seasonal_weights=FLAT,
    ),
    "anxiety_adult": ConditionProfile(
        icd10_code="F41.1",
        description="Generalized anxiety disorder",
        chief_complaint="Anxiety, nervousness, insomnia",
        visit_type="acute",
        hr_delta=(8, 4),
        pain=(2, 1),
        labs=tsh() + cmp(),
        rx_options=ANXIETY_RX,
        follow_up_days=30,
        seasonal_weights=FLAT,
    ),
    "contraception_consult": ConditionProfile(
        icd10_code="Z30.09",
        description="Encounter for other general contraceptive management",
        chief_complaint="Contraception counseling / prescription",
        visit_type="preventive",
        pain=(0, 0),
        labs=[],
        rx_options=[RxSpec("Combined oral contraceptive", "Hormonal contraceptive", "1 tab", "QD", None, 3)],
        follow_up_days=365,
        seasonal_weights=FLAT,
    ),
    # ── Middle Adult (36-65) ──────────────────────────────────────────────────
    "hypertension": ConditionProfile(
        icd10_code="I10",
        description="Essential hypertension",
        chief_complaint="Hypertension follow-up / BP check",
        visit_type="follow_up",
        bp_sys_delta=(28, 10),
        bp_dia_delta=(15, 8),
        hr_delta=(5, 5),
        pain=(0, 1),
        labs=cmp() + [LabSpec("BUN", "3094-0", "mg/dL", 7, 20, 13, 3)],
        rx_options=HTN_RX,
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    "type2_diabetes": ConditionProfile(
        icd10_code="E11.9",
        description="Type 2 diabetes mellitus without complications",
        chief_complaint="Diabetes follow-up / glucose management",
        visit_type="follow_up",
        bp_sys_delta=(15, 8),
        bp_dia_delta=(8, 5),
        pain=(1, 1),
        labs=hba1c() + glucose_stat() + cmp() + lipid_panel(),
        rx_options=DM_RX,
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    "hyperlipidemia": ConditionProfile(
        icd10_code="E78.5",
        description="Hyperlipidemia, unspecified",
        chief_complaint="High cholesterol / lipid management",
        visit_type="follow_up",
        pain=(0, 0),
        labs=lipid_panel() + cmp(),
        rx_options=LIPID_RX,
        follow_up_days=180,
        seasonal_weights=FLAT,
    ),
    "gerd": ConditionProfile(
        icd10_code="K21.9",
        description="Gastro-esophageal reflux disease without esophagitis",
        chief_complaint="Heartburn, acid reflux",
        visit_type="acute",
        pain=(4, 2),
        labs=[],
        rx_options=GERD_RX,
        follow_up_days=30,
        seasonal_weights=FLAT,
    ),
    "osteoarthritis": ConditionProfile(
        icd10_code="M19.90",
        description="Unspecified osteoarthritis, unspecified site",
        chief_complaint="Joint pain / stiffness",
        visit_type="follow_up",
        pain=(5, 2),
        labs=[],
        rx_options=PAIN_RX + [RxSpec("Meloxicam", "COX-2 NSAID", "15mg", "QD", None, 3)],
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    "obesity": ConditionProfile(
        icd10_code="E66.9",
        description="Obesity, unspecified",
        chief_complaint="Weight management counseling",
        visit_type="follow_up",
        pain=(1, 1),
        labs=lipid_panel() + hba1c(),
        rx_options=[RxSpec("Lifestyle counseling referral", "Referral", "—", "—", None, 0)],
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    # ── Senior (65+) ──────────────────────────────────────────────────────────
    "copd": ConditionProfile(
        icd10_code="J44.1",
        description="COPD with acute exacerbation",
        chief_complaint="Shortness of breath, worsening COPD",
        visit_type="follow_up",
        rr_delta=(6, 3),
        spo2_delta=(-5, 2),
        pain=(3, 2),
        labs=cbc() + [LabSpec("FEV1", "19870-5", "%predicted", 0, 100, 58, 15)],
        rx_options=COPD_RX,
        follow_up_days=30,
        seasonal_weights=FLU_SEASON,
    ),
    "fall_injury": ConditionProfile(
        icd10_code="W19.XXXA",
        description="Unspecified fall, initial encounter",
        chief_complaint="Fall / injury at home",
        visit_type="urgent",
        pain=(6, 2),
        labs=bmp(),
        rx_options=PAIN_RX,
        follow_up_days=7,
        seasonal_weights={m: (1.5 if m in (12, 1, 2) else 0.9) for m in range(1, 13)},
    ),
    "polypharmacy_review": ConditionProfile(
        icd10_code="Z87.891",
        description="Medication reconciliation / polypharmacy review",
        chief_complaint="Medication management review",
        visit_type="preventive",
        pain=(0, 0),
        labs=cmp() + tsh(),
        rx_options=[],
        follow_up_days=90,
        seasonal_weights=FLAT,
    ),
    "annual_physical_senior": ConditionProfile(
        icd10_code="Z00.00",
        description="Annual wellness visit (Medicare)",
        chief_complaint="Annual wellness visit",
        visit_type="preventive",
        pain=(0, 0),
        labs=cbc() + cmp() + lipid_panel() + hba1c() + tsh(),
        rx_options=[],
        follow_up_days=365,
        seasonal_weights=FLAT,
    ),
    "depression_senior": ConditionProfile(
        icd10_code="F33.0",
        description="Major depressive disorder, recurrent, mild",
        chief_complaint="Low mood, lack of energy, poor sleep",
        visit_type="follow_up",
        pain=(2, 1),
        labs=tsh() + cmp(),
        rx_options=ANXIETY_RX,
        follow_up_days=30,
        seasonal_weights={m: (1.5 if m in (11, 12, 1, 2) else 0.8) for m in range(1, 13)},
    ),
    "hypothyroidism": ConditionProfile(
        icd10_code="E03.9",
        description="Hypothyroidism, unspecified",
        chief_complaint="Thyroid management / fatigue, weight gain",
        visit_type="follow_up",
        hr_delta=(-8, 3),
        pain=(1, 1),
        labs=tsh(),
        rx_options=[RxSpec("Levothyroxine", "Thyroid hormone replacement", "50mcg", "QD", None, 3)],
        follow_up_days=180,
        seasonal_weights=FLAT,
    ),
}


# ─── Age-Stratified Condition Weights ─────────────────────────────────────────
# Format: { age_group_key: [(condition_name, base_weight), ...] }

AGE_WEIGHTS: dict[str, list[tuple[str, float]]] = {
    "infant": [  # 0–2
        ("well_child", 3.5),
        ("otitis_media", 3.0),
        ("rsv", 2.5),
        ("febrile_illness", 2.0),
        ("rash_eczema", 1.0),
        ("conjunctivitis", 0.8),
        ("uri_adult", 1.5),
    ],
    "child": [  # 3–12
        ("well_child", 2.5),
        ("otitis_media", 3.0),
        ("strep_throat_ped", 2.0),
        ("uri_adult", 2.0),
        ("febrile_illness", 1.5),
        ("rash_eczema", 1.0),
        ("conjunctivitis", 0.8),
        ("sports_injury", 0.5),
    ],
    "teen": [  # 13–17
        ("sports_physical", 2.0),
        ("uri_adult", 1.8),
        ("sports_injury", 1.5),
        ("acne", 1.5),
        ("anxiety_teen", 1.0),
        ("strep_throat_ped", 1.0),
        ("mononucleosis", 0.5),
        ("well_child", 0.5),
        ("influenza", 1.0),
    ],
    "young_adult": [  # 18–35
        ("annual_physical_adult", 2.0),
        ("uri_adult", 2.0),
        ("influenza", 1.5),
        ("uti", 1.5),
        ("anxiety_adult", 1.2),
        ("low_back_pain", 1.0),
        ("minor_laceration", 0.8),
        ("contraception_consult", 0.8),
        ("gerd", 0.5),
        ("sports_injury", 0.8),
    ],
    "adult": [  # 36–50
        ("annual_physical_adult", 2.0),
        ("hypertension", 2.0),
        ("hyperlipidemia", 1.5),
        ("type2_diabetes", 1.0),
        ("uri_adult", 1.5),
        ("influenza", 1.2),
        ("gerd", 1.2),
        ("anxiety_adult", 1.0),
        ("low_back_pain", 1.2),
        ("obesity", 0.8),
        ("uti", 0.8),
        ("hypothyroidism", 0.5),
    ],
    "middle_aged": [  # 51–65
        ("annual_physical_adult", 2.0),
        ("hypertension", 2.5),
        ("type2_diabetes", 2.0),
        ("hyperlipidemia", 2.0),
        ("osteoarthritis", 1.5),
        ("gerd", 1.2),
        ("uri_adult", 1.0),
        ("influenza", 1.2),
        ("copd", 0.8),
        ("depression_senior", 0.8),
        ("hypothyroidism", 0.8),
        ("obesity", 1.0),
    ],
    "senior": [  # 65+
        ("annual_physical_senior", 2.5),
        ("hypertension", 2.5),
        ("type2_diabetes", 2.0),
        ("hyperlipidemia", 1.8),
        ("osteoarthritis", 2.0),
        ("copd", 1.2),
        ("fall_injury", 1.2),
        ("polypharmacy_review", 1.5),
        ("depression_senior", 1.0),
        ("hypothyroidism", 1.0),
        ("influenza", 1.5),
        ("uri_adult", 1.0),
    ],
}


def age_group(age: int) -> str:
    if age <= 2:
        return "infant"
    if age <= 12:
        return "child"
    if age <= 17:
        return "teen"
    if age <= 35:
        return "young_adult"
    if age <= 50:
        return "adult"
    if age <= 65:
        return "middle_aged"
    return "senior"


def pick_condition(age: int, month: int, existing_conditions: set[str]) -> tuple[ConditionProfile, str]:
    """
    Sample a condition for this patient visit, weighted by:
    - Age group base weights
    - Seasonal multiplier for the visit month
    - Comorbidity boost if condition is already established
    """
    group = age_group(age)
    pool = AGE_WEIGHTS[group]

    weights = []
    names = []
    for cname, base_w in pool:
        cond = CONDITIONS[cname]
        seasonal_mult = cond.seasonal_weights.get(month, 1.0)
        # Boost follow-up visits for established chronic conditions
        comorbidity_mult = 1.8 if cname in existing_conditions else 1.0
        # Sex-specific: UTI and contraception more common in females (handled in generator)
        weights.append(base_w * seasonal_mult * comorbidity_mult)
        names.append(cname)

    chosen = random.choices(names, weights=weights, k=1)[0]
    return CONDITIONS[chosen], chosen


def comorbidity_seeds(age: int, fam_hx: dict, smoker: bool, bmi: float) -> set[str]:
    """
    Determine which chronic conditions are seeded for this patient from day 1.
    Returns a set of condition names already 'established'.
    """
    seeds = set()
    if age >= 45:
        if fam_hx.get("hypertension") or random.random() < 0.30:
            seeds.add("hypertension")
        if fam_hx.get("diabetes") or bmi > 27 or random.random() < 0.20:
            seeds.add("type2_diabetes")
        if random.random() < 0.35:
            seeds.add("hyperlipidemia")
    if age >= 60:
        if smoker or random.random() < 0.15:
            seeds.add("copd")
        if random.random() < 0.25:
            seeds.add("hypothyroidism")
        if random.random() < 0.40:
            seeds.add("osteoarthritis")
    return seeds
