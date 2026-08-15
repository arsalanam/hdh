"""The family-medicine-core condition pack (design clinical-breadth.md §4).

Authored clinical content for a typical family physician's OPD: 32
conditions with age/sex/season-weighted occurrence, vitals deltas, lab
panels, formularies, and chronic-onset rules. Contracts and sampling
live in :mod:`hdh.core.conditions`; this module only DEFINES content and
exposes it as :class:`FamilyMedicineCorePack` — adding a condition means
adding one ``_Draft`` entry plus its band weights, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hdh.core.conditions import (
    AgeBand,
    ConditionProfile,
    LabSpec,
    OnsetProfile,
    RiskFactor,
    RiskKind,
    RxSpec,
)
from hdh.core.models import Sex, VisitType

# ─── Authoring shim ──────────────────────────────────────────────────────────


@dataclass
class _Draft:
    """Ergonomic authoring shape for one condition (old-style kwargs,
    mutable lists/dicts); the pack converts drafts to frozen
    ConditionProfiles at assembly. Private — never leaves this module."""

    icd10_code: str
    description: str
    chief_complaint: str
    visit_type: str
    bp_sys_delta: tuple = (0, 5)
    bp_dia_delta: tuple = (0, 3)
    hr_delta: tuple = (0, 5)
    rr_delta: tuple = (0, 2)
    temp_delta: tuple = (0.0, 0.2)
    spo2_delta: tuple = (0, 1)
    pain: tuple = (0, 1)
    labs: list = field(default_factory=list)
    rx_options: list = field(default_factory=list)
    rx_pick_all: bool = False
    follow_up_days: int | None = None
    seasonal_weights: dict = field(default_factory=dict)


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

_DEFINITIONS: dict[str, _Draft] = {
    # ── Pediatric (0-12) ──────────────────────────────────────────────────────
    "otitis_media": _Draft(
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
    "well_child": _Draft(
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
    "rsv": _Draft(
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
    "febrile_illness": _Draft(
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
    "strep_throat_ped": _Draft(
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
    "conjunctivitis": _Draft(
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
    "rash_eczema": _Draft(
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
    "acne": _Draft(
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
    "sports_physical": _Draft(
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
    "sports_injury": _Draft(
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
    "mononucleosis": _Draft(
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
    "anxiety_teen": _Draft(
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
    "annual_physical_adult": _Draft(
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
    "influenza": _Draft(
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
    "uri_adult": _Draft(
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
    "uti": _Draft(
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
    "minor_laceration": _Draft(
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
    "low_back_pain": _Draft(
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
    "anxiety_adult": _Draft(
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
    "contraception_consult": _Draft(
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
    "hypertension": _Draft(
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
    "type2_diabetes": _Draft(
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
    "hyperlipidemia": _Draft(
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
    "gerd": _Draft(
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
    "osteoarthritis": _Draft(
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
    "obesity": _Draft(
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
    "copd": _Draft(
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
    "fall_injury": _Draft(
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
    "polypharmacy_review": _Draft(
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
    "annual_physical_senior": _Draft(
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
    "depression_senior": _Draft(
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
    "hypothyroidism": _Draft(
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

_BAND_WEIGHTS: dict[str, list[tuple[str, float]]] = {
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


# ─── Pack metadata (one place per concern; the profile absorbs them) ─────────

_CHRONIC = frozenset(
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

_SEX_LIMIT = {"uti": Sex.FEMALE, "contraception_consult": Sex.FEMALE}

# Opportunistic SNOMED codes for the well-known conditions (design §10 Q3)
_SNOMED = {
    "hypertension": "59621000",
    "type2_diabetes": "44054006",
    "hyperlipidemia": "55822004",
    "copd": "13645005",
    "osteoarthritis": "396275006",
    "hypothyroidism": "40930008",
    "obesity": "414916001",
    "gerd": "235595009",
    "influenza": "6142004",
    "uti": "68566005",
    "anxiety_adult": "21897009",
    "anxiety_teen": "21897009",
    "depression_senior": "35489007",
    "low_back_pain": "279039007",
}

# Chart-start onset rules — the legacy comorbidity_seeds table, declarative
_ONSETS = {
    "hypertension": OnsetProfile(min_age=45, baseline_probability=0.30, hereditary_key="hypertension"),
    "type2_diabetes": OnsetProfile(
        min_age=45,
        baseline_probability=0.20,
        hereditary_key="diabetes",
        force_factors=(RiskFactor(RiskKind.BMI_OVER, 27),),
    ),
    "hyperlipidemia": OnsetProfile(min_age=45, baseline_probability=0.35),
    "copd": OnsetProfile(min_age=60, baseline_probability=0.15, force_factors=(RiskFactor(RiskKind.SMOKER),)),
    "hypothyroidism": OnsetProfile(min_age=60, baseline_probability=0.25),
    "osteoarthritis": OnsetProfile(min_age=60, baseline_probability=0.40),
}


class FamilyMedicineCorePack:
    """The core OPD condition set as a pluggable ConditionSource (§4)."""

    name = "family-medicine-core"

    def conditions(self) -> tuple[ConditionProfile, ...]:
        """Assemble every draft + its metadata into a frozen profile."""
        band_weights: dict[str, list[tuple[AgeBand, float]]] = {}
        for band_name, pool in _BAND_WEIGHTS.items():
            for cname, weight in pool:
                band_weights.setdefault(cname, []).append((AgeBand(band_name), weight))
        return tuple(
            ConditionProfile(
                name=cname,
                icd10_code=draft.icd10_code,
                description=draft.description,
                chief_complaint=draft.chief_complaint,
                visit_type=VisitType(draft.visit_type),
                chronic=cname in _CHRONIC,
                snomed_code=_SNOMED.get(cname),
                sex_limit=_SEX_LIMIT.get(cname),
                bp_sys_delta=tuple(draft.bp_sys_delta),
                bp_dia_delta=tuple(draft.bp_dia_delta),
                hr_delta=tuple(draft.hr_delta),
                rr_delta=tuple(draft.rr_delta),
                temp_delta=tuple(draft.temp_delta),
                spo2_delta=tuple(draft.spo2_delta),
                pain=tuple(draft.pain),
                labs=tuple(draft.labs),
                rx_options=tuple(draft.rx_options),
                rx_pick_all=draft.rx_pick_all,
                follow_up_days=draft.follow_up_days,
                seasonal_weights=tuple((m, w) for m, w in sorted(draft.seasonal_weights.items()) if w != 1.0),
                visit_weights=tuple(band_weights.get(cname, ())),
                onset=_ONSETS.get(cname),
            )
            for cname, draft in _DEFINITIONS.items()
        )
