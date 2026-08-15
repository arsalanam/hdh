"""The cardiometabolic condition pack (design clinical-breadth.md §6).

The clinical breadth issue #28 asked for: seven conditions whose onset
is driven by the comorbidity webs (CKD *because of* hypertension and
diabetes, stroke history *because of* atrial fibrillation), each with
ICD-10 **and** SNOMED codes, drug and lab profiles, and follow-up
cadences. CKD carries the first :class:`StageProfile` — a 4-year chart
now shows a trajectory, not a frozen stage.

Prevalence targets are *plausible for a family-medicine panel*, not
epidemiological claims (design §10 Q2); the statistical tests assert
sanity ranges, never truth.
"""

from __future__ import annotations

from hdh.core.conditions import (
    AgeBand,
    ComorbidityLink,
    ConditionProfile,
    LabSpec,
    OnsetProfile,
    RiskFactor,
    RiskKind,
    RxSpec,
    Stage,
    StageProfile,
)
from hdh.core.models import VisitType

# ── Lab panels ───────────────────────────────────────────────────────────────


def _renal_panel() -> tuple[LabSpec, ...]:
    return (
        LabSpec(
            "Creatinine", "2160-0", "mg/dL", 0.6, 1.2, 0.9, 0.15, condition_shift=0.8, condition_shift_sd=0.4
        ),
        LabSpec(
            "eGFR", "62238-1", "mL/min/1.73m2", 60, 120, 90, 10, condition_shift=-38, condition_shift_sd=12
        ),
        LabSpec(
            "Potassium", "2823-3", "mEq/L", 3.5, 5.0, 4.1, 0.3, condition_shift=0.3, condition_shift_sd=0.3
        ),
    )


def _lipid_panel() -> tuple[LabSpec, ...]:
    return (
        LabSpec(
            "Total Cholesterol", "2093-3", "mg/dL", 0, 200, 185, 30, condition_shift=30, condition_shift_sd=20
        ),
        LabSpec("LDL", "2089-1", "mg/dL", 0, 100, 110, 25, condition_shift=30, condition_shift_sd=18),
        LabSpec("HDL", "2085-9", "mg/dL", 40, 60, 50, 10, condition_shift=-8, condition_shift_sd=5),
    )


def _bnp() -> tuple[LabSpec, ...]:
    return (LabSpec("BNP", "30934-4", "pg/mL", 0, 100, 40, 20, condition_shift=320, condition_shift_sd=180),)


def _cbc_ferritin() -> tuple[LabSpec, ...]:
    return (
        LabSpec(
            "Hemoglobin", "718-7", "g/dL", 13.5, 17.5, 15.0, 1.2, condition_shift=-3.5, condition_shift_sd=1.0
        ),
        LabSpec("Ferritin", "2276-4", "ng/mL", 30, 300, 100, 40, condition_shift=-80, condition_shift_sd=25),
    )


def _inr() -> tuple[LabSpec, ...]:
    return (
        LabSpec("INR", "6301-6", "ratio", 0.8, 1.2, 1.0, 0.1, condition_shift=1.4, condition_shift_sd=0.4),
    )


# ── The pack ─────────────────────────────────────────────────────────────────


def _ckd() -> ConditionProfile:
    """The ckd profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="ckd",
        icd10_code="N18.31",
        snomed_code="700378005",
        description="Chronic kidney disease, stage 3a",
        chief_complaint="CKD follow-up / declining renal function",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        labs=_renal_panel(),
        rx_options=(RxSpec("Lisinopril", "ACE inhibitor (renoprotective)", "10mg", "QD", None, 3),),
        follow_up_days=120,
        visit_weights=((AgeBand.MIDDLE_AGED, 0.5), (AgeBand.SENIOR, 0.9)),
        onset=OnsetProfile(
            min_age=50,
            baseline_probability=0.0,  # arrives only through the webs
            annual_rate=0.004,
            comorbid_links=(
                ComorbidityLink("hypertension", 3.0),
                ComorbidityLink("type2_diabetes", 3.0),
            ),
        ),
        staging=StageProfile(
            stages=(
                Stage("N18.31", "Chronic kidney disease, stage 3a", "700378005"),
                Stage("N18.32", "Chronic kidney disease, stage 3b", "700379002"),
                Stage("N18.4", "Chronic kidney disease, stage 4", "431857002"),
                Stage("N18.5", "Chronic kidney disease, stage 5", "433146000"),
            ),
            progress_probability=0.18,
            improve_probability=0.05,
        ),
    )


def _cad() -> ConditionProfile:
    """The cad profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="cad",
        icd10_code="I25.10",
        snomed_code="53741008",
        description="Coronary artery disease without angina pectoris",
        chief_complaint="CAD follow-up / exertional chest tightness",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        bp_sys_delta=(8, 6),
        labs=_lipid_panel(),
        rx_options=(
            RxSpec("Atorvastatin", "Statin (high-intensity)", "40mg", "QD", None, 3),
            RxSpec("Aspirin", "Antiplatelet", "81mg", "QD", None, 3),
            RxSpec("Metoprolol succinate", "Beta blocker", "50mg", "QD", None, 3),
        ),
        rx_pick_all=True,  # secondary prevention is a regimen, not a choice
        follow_up_days=180,
        visit_weights=((AgeBand.MIDDLE_AGED, 0.5), (AgeBand.SENIOR, 0.8)),
        onset=OnsetProfile(
            min_age=50,
            baseline_probability=0.0,
            annual_rate=0.006,
            force_factors=(RiskFactor(RiskKind.SMOKER, multiplier=2.0),),
            comorbid_links=(
                ComorbidityLink("hypertension", 2.0),
                ComorbidityLink("hyperlipidemia", 2.5),
                ComorbidityLink("type2_diabetes", 1.8),
            ),
        ),
    )


def _heart_failure() -> ConditionProfile:
    """The heart_failure profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="heart_failure",
        icd10_code="I50.32",
        snomed_code="84114007",
        description="Chronic diastolic heart failure",
        chief_complaint="Dyspnea on exertion / ankle swelling",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        hr_delta=(8, 5),
        spo2_delta=(-2, 1),
        labs=_bnp(),
        rx_options=(
            RxSpec("Furosemide", "Loop diuretic", "40mg", "QD", None, 3),
            RxSpec("Lisinopril", "ACE inhibitor", "10mg", "QD", None, 3),
            RxSpec("Metoprolol succinate", "Beta blocker", "25mg", "QD", None, 3),
        ),
        rx_pick_all=True,
        follow_up_days=90,
        visit_weights=((AgeBand.SENIOR, 0.7),),
        onset=OnsetProfile(
            min_age=60,
            baseline_probability=0.0,
            annual_rate=0.004,
            comorbid_links=(ComorbidityLink("cad", 4.0), ComorbidityLink("hypertension", 2.0)),
        ),
    )


def _afib() -> ConditionProfile:
    """The afib profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="afib",
        icd10_code="I48.91",
        snomed_code="49436004",
        description="Unspecified atrial fibrillation",
        chief_complaint="Palpitations / irregular heartbeat",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        hr_delta=(25, 15),
        labs=_inr(),
        rx_options=(
            RxSpec("Apixaban", "Anticoagulant (DOAC)", "5mg", "BID", None, 3),
            RxSpec("Warfarin", "Anticoagulant (VKA)", "5mg", "QD", None, 3),
        ),
        follow_up_days=90,
        visit_weights=((AgeBand.SENIOR, 0.7),),
        onset=OnsetProfile(
            min_age=60,
            baseline_probability=0.0,
            annual_rate=0.006,
            comorbid_links=(
                ComorbidityLink("hypertension", 1.8),
                ComorbidityLink("heart_failure", 2.0),
            ),
        ),
    )


def _stroke_history() -> ConditionProfile:
    """The stroke_history profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="stroke_history",
        icd10_code="Z86.73",
        snomed_code="275526006",
        description="Personal history of TIA and cerebral infarction",
        chief_complaint="Post-stroke follow-up / risk-factor management",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        labs=_lipid_panel(),
        rx_options=(
            RxSpec("Clopidogrel", "Antiplatelet", "75mg", "QD", None, 3),
            RxSpec("Aspirin", "Antiplatelet", "81mg", "QD", None, 3),
        ),
        follow_up_days=180,
        visit_weights=((AgeBand.SENIOR, 0.5),),
        onset=OnsetProfile(
            min_age=60,
            baseline_probability=0.0,
            annual_rate=0.002,
            comorbid_links=(
                ComorbidityLink("afib", 4.0),
                ComorbidityLink("hypertension", 2.0),
                ComorbidityLink("cad", 1.5),
            ),
        ),
    )


def _asthma() -> ConditionProfile:
    """The asthma profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="asthma",
        icd10_code="J45.909",
        snomed_code="195967001",
        description="Unspecified asthma, uncomplicated",
        chief_complaint="Wheezing / chest tightness",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        rr_delta=(4, 2),
        spo2_delta=(-2, 1),
        rx_options=(
            RxSpec("Albuterol inhaler", "SABA rescue", "2 puffs", "Q4H PRN", None, 3),
            RxSpec("Fluticasone inhaler", "Inhaled corticosteroid", "110mcg", "BID", None, 3),
        ),
        rx_pick_all=True,
        follow_up_days=180,
        seasonal_weights=((3, 1.4), (4, 1.6), (5, 1.4), (9, 1.5), (10, 1.4)),
        visit_weights=(
            (AgeBand.CHILD, 1.0),
            (AgeBand.TEEN, 0.9),
            (AgeBand.YOUNG_ADULT, 0.7),
            (AgeBand.ADULT, 0.5),
        ),
        onset=OnsetProfile(min_age=5, baseline_probability=0.08, annual_rate=0.002),
    )


def _anemia_iron() -> ConditionProfile:
    """The anemia_iron profile (design clinical-breadth.md section 6)."""
    return ConditionProfile(
        name="anemia_iron",
        icd10_code="D50.9",
        snomed_code="87522002",
        description="Iron deficiency anemia, unspecified",
        chief_complaint="Fatigue / pallor",
        visit_type=VisitType.FOLLOW_UP,
        chronic=True,
        hr_delta=(6, 4),
        labs=_cbc_ferritin(),
        rx_options=(RxSpec("Ferrous sulfate", "Iron supplement", "325mg", "QD", 90, 2),),
        follow_up_days=90,
        visit_weights=((AgeBand.YOUNG_ADULT, 0.4), (AgeBand.ADULT, 0.4), (AgeBand.SENIOR, 0.4)),
        onset=OnsetProfile(
            min_age=18,
            baseline_probability=0.0,
            annual_rate=0.003,
            female_multiplier=2.0,
            comorbid_links=(ComorbidityLink("ckd", 2.0),),
        ),
    )


class CardiometabolicPack:
    """Cardio/renal/respiratory chronic disease with real comorbidity webs."""

    name = "cardiometabolic"

    def conditions(self) -> tuple[ConditionProfile, ...]:
        """The seven section-6 conditions, one authoring function each."""
        return (_ckd(), _cad(), _heart_failure(), _afib(), _stroke_history(), _asthma(), _anemia_iron())
