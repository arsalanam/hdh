"""Deterministic SOAP note rendering — in core so every generated visit
stores its note as a VisitNote row (design core-chart-expansion §6).

Two entry points, one text format:

- :func:`render_soap` is **pure** — it takes plain values and never touches
  the ORM. The generator uses it with the in-memory objects it just built,
  so note rendering costs zero queries (the N+1 this replaced was 36% of
  generation time).
- :func:`visit_to_soap` walks a loaded ORM chart and delegates — the
  narrative module's presentation path.
"""

from hdh.core.models import Patient, Visit


def render_soap(
    *,
    provider_name: str,
    visit_date,
    chief_complaint: str | None,
    follow_up_days: int | None,
    age: int,
    sex: str,
    allergies: list[str],
    chronic_history: list[str],
    family_history: list[str],
    vital,
    conditions: list[tuple[str, str]],  # (description, icd10_code)
    prescriptions: list[dict],  # drug_name, dose, frequency, duration_days, is_new
    labs: list[tuple[str, float | None, str | None, str]],  # name, value, unit, status
    procedures: list[str],
) -> str:
    """Render one SOAP note from plain values (no ORM, no queries)."""
    s_lines = [f"{age}-year-old {sex} presents with: {chief_complaint}."]
    if allergies:
        s_lines.append(f"Known allergies: {', '.join(allergies)}.")
    if chronic_history:
        s_lines.append(f"History of: {', '.join(chronic_history)}.")
    if family_history:
        s_lines.append(f"Family history: {'; '.join(family_history[:3])}.")

    o_parts = []
    if vital is not None:
        o_parts.append(
            f"Vitals: BP {vital.bp_systolic}/{vital.bp_diastolic} mmHg, HR {vital.heart_rate}, "
            f"RR {vital.respiratory_rate}, T {vital.temperature_f}°F, SpO2 {vital.oxygen_sat}%, "
            f"BMI {vital.bmi}, pain {vital.pain_scale}/10."
        )
    abnormal = [
        f"{name} {value} {unit} ({status.lower()})"
        for name, value, unit, status in labs
        if status.lower() != "normal"
    ]
    if abnormal:
        o_parts.append("Notable labs: " + "; ".join(abnormal) + ".")
    elif labs:
        o_parts.append("All ordered labs within normal limits.")
    objective = " ".join(o_parts) or "No vitals recorded."

    assessment = "; ".join(f"{desc} ({code})" for desc, code in conditions) or "No diagnosis recorded."

    p_parts = []
    for rx in prescriptions:
        status = "Start" if rx.get("is_new") else "Continue"
        dur = f" x{rx['duration_days']} days" if rx.get("duration_days") else ""
        p_parts.append(f"{status} {rx['drug_name']} {rx['dose']} {rx['frequency']}{dur}.")
    for proc in procedures:
        p_parts.append(f"Procedure performed: {proc}.")
    if follow_up_days:
        p_parts.append(f"Follow up in {follow_up_days} days.")
    else:
        p_parts.append("Follow up as needed.")

    return "\n".join(
        [
            f"SOAP NOTE — {visit_date}  ({provider_name})",
            f"S: {' '.join(s_lines)}",
            f"O: {objective}",
            f"A: {assessment}",
            f"P: {' '.join(p_parts)}",
        ]
    )


def visit_to_soap(visit: Visit, patient: Patient) -> str:
    """Render a loaded ORM visit as a SOAP note (delegates to render_soap)."""
    return render_soap(
        provider_name=visit.provider.name if visit.provider else "Unassigned",
        visit_date=visit.visit_date,
        chief_complaint=visit.chief_complaint,
        follow_up_days=visit.follow_up_days,
        age=patient.age,
        sex="male" if str(patient.sex).endswith("M") else "female",
        allergies=[a.substance for a in patient.allergies],
        chronic_history=[
            c.description for c in patient.conditions if c.chronic and str(c.status).endswith("ACTIVE")
        ],
        family_history=[f"{h.relationship_type}: {h.condition}" for h in patient.family_history],
        vital=visit.vitals,
        conditions=[(c.description, c.icd10_code) for c in visit.conditions],
        prescriptions=[
            {
                "drug_name": rx.drug_name,
                "dose": rx.dose,
                "frequency": rx.frequency,
                "duration_days": rx.duration_days,
                "is_new": rx.is_new,
            }
            for rx in visit.prescriptions
        ],
        labs=[(lr.test_name, lr.value, lr.unit, str(lr.status).split(".")[-1]) for lr in visit.lab_results],
        procedures=[p.description for p in visit.procedures],
    )
