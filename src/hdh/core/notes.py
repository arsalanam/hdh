"""Deterministic SOAP note rendering — in core so every generated visit
stores its note as a VisitNote row (design core-chart-expansion §6).

The narrative module remains the presentation layer (and optional LLM
polish); this renderer is the source of the stored text and therefore of
the comprehension service's evaluation corpus.
"""

from hdh.core.models import Patient, Visit


def _subjective(visit: Visit, patient: Patient) -> str:
    age = patient.age
    sex = "male" if str(patient.sex).endswith("M") else "female"
    lines = [f"{age}-year-old {sex} presents with: {visit.chief_complaint}."]
    allergies = [a.substance for a in patient.allergies]
    if allergies:
        lines.append(f"Known allergies: {', '.join(allergies)}.")
    chronic = [c.description for c in patient.conditions if c.chronic and str(c.status).endswith("ACTIVE")]
    if chronic:
        lines.append(f"History of: {', '.join(chronic)}.")
    family = [f"{h.relationship_type}: {h.condition}" for h in patient.family_history[:3]]
    if family:
        lines.append(f"Family history: {'; '.join(family)}.")
    return " ".join(lines)


def _objective(visit: Visit) -> str:
    parts = []
    v = visit.vitals
    if v:
        parts.append(
            f"Vitals: BP {v.bp_systolic}/{v.bp_diastolic} mmHg, HR {v.heart_rate}, "
            f"RR {v.respiratory_rate}, T {v.temperature_f}°F, SpO2 {v.oxygen_sat}%, "
            f"BMI {v.bmi}, pain {v.pain_scale}/10."
        )
    abnormal = [
        f"{lr.test_name} {lr.value} {lr.unit} ({str(lr.status).split('.')[-1].lower()})"
        for lr in visit.lab_results
        if str(lr.status).split(".")[-1].lower() != "normal"
    ]
    if abnormal:
        parts.append("Notable labs: " + "; ".join(abnormal) + ".")
    elif visit.lab_results:
        parts.append("All ordered labs within normal limits.")
    return " ".join(parts) or "No vitals recorded."


def _assessment(visit: Visit) -> str:
    dx = [f"{c.description} ({c.icd10_code})" for c in visit.conditions]
    return "; ".join(dx) if dx else "No diagnosis recorded."


def _plan(visit: Visit) -> str:
    parts = []
    for rx in visit.prescriptions:
        status = "Start" if rx.is_new else "Continue"
        dur = f" x{rx.duration_days} days" if rx.duration_days else ""
        parts.append(f"{status} {rx.drug_name} {rx.dose} {rx.frequency}{dur}.")
    for proc in visit.procedures:
        parts.append(f"Procedure performed: {proc.description}.")
    if visit.follow_up_days:
        parts.append(f"Follow up in {visit.follow_up_days} days.")
    else:
        parts.append("Follow up as needed.")
    return " ".join(parts)


def visit_to_soap(visit: Visit, patient: Patient) -> str:
    """Render one visit as a deterministic SOAP note."""
    provider = visit.provider.name if visit.provider else "Unassigned"
    return "\n".join(
        [
            f"SOAP NOTE — {visit.visit_date}  ({provider})",
            f"S: {_subjective(visit, patient)}",
            f"O: {_objective(visit)}",
            f"A: {_assessment(visit)}",
            f"P: {_plan(visit)}",
        ]
    )
