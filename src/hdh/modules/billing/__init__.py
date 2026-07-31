"""Billing simulation (scaffold): CPT codes and RVUs per visit.

Provides evaluation & management (E/M) CPT assignment from visit type and
patient age, with work RVUs — the starting point for claims simulation.
"""

from hdh.core.models import Visit, VisitType

# (cpt, work_rvu) for E/M visit codes
_ESTABLISHED_OFFICE = ("99213", 1.30)
_ESTABLISHED_COMPLEX = ("99214", 1.92)
_URGENT = ("99215", 2.80)

_PREVENTIVE_BY_AGE = [
    (1,   ("99381", 1.50)),   # infant
    (5,   ("99382", 1.60)),
    (12,  ("99383", 1.70)),
    (18,  ("99384", 2.00)),
    (40,  ("99385", 1.92)),
    (65,  ("99386", 2.33)),
    (200, ("99387", 2.50)),
]


def cpt_for_visit(visit: Visit, patient_age: int) -> tuple[str, float]:
    """Return (cpt_code, work_rvu) for a visit."""
    vtype = visit.visit_type
    if vtype == VisitType.PREVENTIVE:
        for max_age, code in _PREVENTIVE_BY_AGE:
            if patient_age < max_age:
                return code
    if vtype == VisitType.URGENT:
        return _URGENT
    # Follow-ups with multiple problems / prescriptions code higher
    if vtype == VisitType.FOLLOW_UP and len(visit.prescriptions) >= 2:
        return _ESTABLISHED_COMPLEX
    return _ESTABLISHED_OFFICE


def estimate_claim(visit: Visit, patient_age: int,
                   conversion_factor: float = 33.29) -> dict:
    """Rough claim estimate for a visit (CMS conversion factor default)."""
    cpt, rvu = cpt_for_visit(visit, patient_age)
    return {
        "visit_date": str(visit.visit_date),
        "cpt": cpt,
        "work_rvu": rvu,
        "estimated_charge_usd": round(rvu * conversion_factor, 2),
    }
