"""
Care-gap detection rules.

All rules are evaluated against a reference date ("as of"). Because a generated
dataset has a fixed time window, the default reference date is the latest visit
date in the database rather than the wall clock — so gap detection stays
meaningful no matter when the dataset was generated.
"""

from dataclasses import dataclass, asdict
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from hdh.core.models import Patient, Visit, ChronicCondition, Prescription, VisitType

# Grace multiplier applied to a visit's follow_up_days before it counts as missed
FOLLOW_UP_GRACE = 1.5

# Preventive-visit intervals by age (days)
PREVENTIVE_INTERVALS = [
    (2,   183),   # under 2: well-child every ~6 months
    (120, 365),   # everyone else: annual
]

POLYPHARMACY_MIN_DRUGS = 5
POLYPHARMACY_REVIEW_WINDOW = 183   # days since last visit of any kind


@dataclass
class CareGap:
    mrn: str
    patient_name: str
    age: int
    gap_type: str        # overdue_preventive | uncontrolled_chronic | missed_follow_up | polypharmacy_review
    severity: str        # high | medium | low
    description: str
    overdue_days: int

    def to_dict(self) -> dict:
        return asdict(self)


def reference_date(session: Session) -> date:
    """Latest visit date in the dataset — the default 'as of' for all rules."""
    latest = session.query(func.max(Visit.visit_date)).scalar()
    return latest or date.today()


def _age_on(dob: date, as_of: date) -> int:
    return as_of.year - dob.year - ((as_of.month, as_of.day) < (dob.month, dob.day))


def _preventive_interval(age: int) -> int:
    for max_age, days in PREVENTIVE_INTERVALS:
        if age < max_age:
            return days
    return 365


def detect_gaps(session: Session, mrn: str = None, limit: int = None,
                as_of: date = None) -> list[CareGap]:
    """Run all care-gap rules and return one CareGap per (patient, rule) hit."""
    as_of = as_of or reference_date(session)

    # ── Aggregate lookups (one query each, keyed by patient_id) ──────────────
    last_visit = dict(
        session.query(Visit.patient_id, func.max(Visit.visit_date))
        .group_by(Visit.patient_id).all()
    )
    last_preventive = dict(
        session.query(Visit.patient_id, func.max(Visit.visit_date))
        .filter(Visit.visit_type == VisitType.PREVENTIVE)
        .group_by(Visit.patient_id).all()
    )
    # follow_up_days of each patient's latest visit
    latest_follow_up = {}
    for pid, vdate, fu in session.query(
        Visit.patient_id, Visit.visit_date, Visit.follow_up_days
    ).all():
        cur = latest_follow_up.get(pid)
        if cur is None or vdate > cur[0]:
            latest_follow_up[pid] = (vdate, fu)

    uncontrolled = {}
    for pid, desc in (
        session.query(ChronicCondition.patient_id, ChronicCondition.description)
        .filter(ChronicCondition.controlled.is_(False)).all()
    ):
        uncontrolled.setdefault(pid, []).append(desc)

    year_ago = as_of - timedelta(days=365)
    drug_counts = dict(
        session.query(Visit.patient_id, func.count(func.distinct(Prescription.drug_name)))
        .join(Prescription, Prescription.visit_id == Visit.id)
        .filter(Visit.visit_date >= year_ago)
        .group_by(Visit.patient_id).all()
    )

    # ── Evaluate rules per patient ───────────────────────────────────────────
    q = session.query(Patient)
    if mrn:
        q = q.filter(Patient.mrn == mrn)
    patients = q.all()

    gaps: list[CareGap] = []
    for p in patients:
        age = _age_on(p.date_of_birth, as_of)
        name = f"{p.first_name} {p.last_name}"
        lv = last_visit.get(p.id)

        # Rule 1 — overdue preventive visit
        interval = _preventive_interval(age)
        lp = last_preventive.get(p.id)
        due = (lp or (lv or as_of - timedelta(days=interval + 1))) + timedelta(days=interval)
        if lp is None or (as_of - lp).days > interval:
            overdue = (as_of - due).days if lp else interval
            gaps.append(CareGap(
                mrn=p.mrn, patient_name=name, age=age,
                gap_type="overdue_preventive", severity="low",
                description=(f"No preventive visit in {(as_of - lp).days} days "
                             f"(interval: {interval}d)" if lp
                             else "No preventive visit on record"),
                overdue_days=max(0, overdue),
            ))

        # Rule 2 — uncontrolled chronic condition without recent follow-up
        if p.id in uncontrolled:
            days_since = (as_of - lv).days if lv else 10_000
            if days_since > 90:
                conds = ", ".join(uncontrolled[p.id])
                gaps.append(CareGap(
                    mrn=p.mrn, patient_name=name, age=age,
                    gap_type="uncontrolled_chronic", severity="high",
                    description=(f"Uncontrolled: {conds} — no visit in "
                                 f"{days_since} days"),
                    overdue_days=days_since - 90,
                ))

        # Rule 3 — missed scheduled follow-up
        lf = latest_follow_up.get(p.id)
        if lf and lf[1]:
            visit_date, fu_days = lf
            deadline = visit_date + timedelta(days=int(fu_days * FOLLOW_UP_GRACE))
            if as_of > deadline:
                gaps.append(CareGap(
                    mrn=p.mrn, patient_name=name, age=age,
                    gap_type="missed_follow_up", severity="medium",
                    description=(f"Follow-up in {fu_days}d requested on "
                                 f"{visit_date} — not seen since"),
                    overdue_days=(as_of - deadline).days,
                ))

        # Rule 4 — senior polypharmacy without recent review
        n_drugs = drug_counts.get(p.id, 0)
        if age >= 65 and n_drugs >= POLYPHARMACY_MIN_DRUGS and lv:
            days_since = (as_of - lv).days
            if days_since > POLYPHARMACY_REVIEW_WINDOW:
                gaps.append(CareGap(
                    mrn=p.mrn, patient_name=name, age=age,
                    gap_type="polypharmacy_review", severity="medium",
                    description=(f"{n_drugs} distinct medications in the last year, "
                                 f"no visit in {days_since} days"),
                    overdue_days=days_since - POLYPHARMACY_REVIEW_WINDOW,
                ))

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda g: (severity_rank[g.severity], -g.overdue_days))
    if limit:
        gaps = gaps[:limit]
    return gaps
