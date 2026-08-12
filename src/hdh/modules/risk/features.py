"""
Feature extraction for risk stratification.

For a given cutoff date, features are computed from the 12 months *before*
the cutoff, and the label from the horizon *after* it:

    label = 1  if the patient has an urgent visit OR a critical lab result
               within `horizon_days` after the cutoff, else 0.

At scoring time the cutoff is simply the latest visit date in the dataset
(no label exists yet — that is what the model predicts).
"""

from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from hdh.core.models import (
    Condition,
    LabResult,
    LabStatus,
    Patient,
    Prescription,
    Visit,
    VisitType,
    Vital,
)

LOOKBACK_DAYS = 365
DEFAULT_HORIZON_DAYS = 180

FEATURE_NAMES = (
    "age",
    "sex_male",
    "smoker",
    "bmi_baseline",
    "fam_hx_count",
    "n_chronic",
    "n_uncontrolled",
    "visits_12mo",
    "urgent_visits_12mo",
    "acute_visits_12mo",
    "distinct_drugs_12mo",
    "high_labs_12mo",
    "critical_labs_12mo",
    "mean_bp_sys",
    "max_bp_sys",
    "min_spo2",
    "mean_pain",
)


def extract_features(
    session: Session, cutoff: date, horizon_days: int = DEFAULT_HORIZON_DAYS, with_labels: bool = True
):
    """Return (mrns, X_rows, y) — y is None when with_labels is False."""
    lb_start = cutoff - timedelta(days=LOOKBACK_DAYS)
    hz_end = cutoff + timedelta(days=horizon_days)

    def in_lookback(q):
        return q.filter(Visit.visit_date > lb_start, Visit.visit_date <= cutoff)

    # ── Lookback aggregates, keyed by patient_id ─────────────────────────────
    visit_counts: dict[int, dict] = {}
    for pid, vtype, cnt in (
        in_lookback(session.query(Visit.patient_id, Visit.visit_type, func.count(Visit.id)))
        .group_by(Visit.patient_id, Visit.visit_type)
        .all()
    ):
        visit_counts.setdefault(pid, {})[vtype] = cnt

    lab_counts: dict[int, dict] = {}
    for pid, status, cnt in (
        in_lookback(
            session.query(Visit.patient_id, LabResult.status, func.count(LabResult.id)).join(
                LabResult, LabResult.visit_id == Visit.id
            )
        )
        .group_by(Visit.patient_id, LabResult.status)
        .all()
    ):
        lab_counts.setdefault(pid, {})[status] = cnt

    drug_counts = dict(
        in_lookback(
            session.query(Visit.patient_id, func.count(func.distinct(Prescription.drug_name))).join(
                Prescription, Prescription.visit_id == Visit.id
            )
        )
        .group_by(Visit.patient_id)
        .all()
    )

    vitals_agg = {}
    for pid, mean_sys, max_sys, min_spo2, mean_pain in (
        in_lookback(
            session.query(
                Visit.patient_id,
                func.avg(Vital.bp_systolic),
                func.max(Vital.bp_systolic),
                func.min(Vital.oxygen_sat),
                func.avg(Vital.pain_scale),
            ).join(Vital, Vital.visit_id == Visit.id)
        )
        .group_by(Visit.patient_id)
        .all()
    ):
        vitals_agg[pid] = (mean_sys, max_sys, min_spo2, mean_pain)

    chronic: dict[int, tuple[int, int]] = {}
    for pid, controlled, cnt in (
        session.query(Condition.patient_id, Condition.controlled, func.count(Condition.id))
        .filter(Condition.chronic.is_(True))
        .group_by(Condition.patient_id, Condition.controlled)
        .all()
    ):
        total, unc = chronic.get(pid, (0, 0))
        chronic[pid] = (total + cnt, unc + (0 if controlled else cnt))

    # ── Labels from the horizon window ───────────────────────────────────────
    positives = set()
    if with_labels:
        for (pid,) in (
            session.query(Visit.patient_id)
            .filter(
                Visit.visit_date > cutoff, Visit.visit_date <= hz_end, Visit.visit_type == VisitType.URGENT
            )
            .distinct()
            .all()
        ):
            positives.add(pid)
        for (pid,) in (
            session.query(Visit.patient_id)
            .join(LabResult, LabResult.visit_id == Visit.id)
            .filter(
                Visit.visit_date > cutoff, Visit.visit_date <= hz_end, LabResult.status == LabStatus.CRITICAL
            )
            .distinct()
            .all()
        ):
            positives.add(pid)

    # ── Assemble one row per patient ─────────────────────────────────────────
    mrns, rows, labels = [], [], []
    for p in session.query(Patient).all():
        age = (
            cutoff.year
            - p.date_of_birth.year
            - ((cutoff.month, cutoff.day) < (p.date_of_birth.month, p.date_of_birth.day))
        )
        vc = visit_counts.get(p.id, {})
        lc = lab_counts.get(p.id, {})
        total_ch, unc_ch = chronic.get(p.id, (0, 0))
        mean_sys, max_sys, min_spo2, mean_pain = vitals_agg.get(p.id, (120.0, 120.0, 98.0, 0.0))
        # family-history burden from the structured FamilyHistory rows
        fam_hx = min(4, len(p.family_history))

        rows.append(
            [
                age,
                1 if str(p.sex).endswith("M") or "MALE" in str(p.sex) else 0,
                1 if p.smoker else 0,
                p.bmi_baseline or 25.0,
                fam_hx,
                total_ch,
                unc_ch,
                sum(vc.values()),
                vc.get(VisitType.URGENT, 0),
                vc.get(VisitType.ACUTE, 0),
                drug_counts.get(p.id, 0),
                lc.get(LabStatus.HIGH, 0),
                lc.get(LabStatus.CRITICAL, 0),
                float(mean_sys or 120.0),
                float(max_sys or 120.0),
                float(min_spo2 or 98.0),
                float(mean_pain or 0.0),
            ]
        )
        mrns.append(p.mrn)
        labels.append(1 if p.id in positives else 0)

    return mrns, rows, (labels if with_labels else None)
