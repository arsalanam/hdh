"""
Template-based SOAP note rendering for visits.
"""

from hdh.core.models import Patient, Visit


def _subjective(visit: Visit, patient: Patient) -> str:
    age = patient.age
    sex = "male" if str(patient.sex).endswith("M") else "female"
    lines = [f"{age}-year-old {sex} presents with: {visit.chief_complaint}."]
    if patient.allergies and patient.allergies != "NKDA":
        lines.append(f"Known allergies: {patient.allergies.replace('|', ', ')}.")
    chronic = [c.description for c in patient.chronic_conditions]
    if chronic:
        lines.append(f"History of: {', '.join(chronic)}.")
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
    dx = [f"{d.description} ({d.icd10_code})" for d in visit.diagnoses]
    return "; ".join(dx) if dx else "No diagnosis recorded."


def _plan(visit: Visit) -> str:
    parts = []
    for rx in visit.prescriptions:
        status = "Start" if rx.is_new else "Continue"
        dur = f" x{rx.duration_days} days" if rx.duration_days else ""
        parts.append(f"{status} {rx.drug_name} {rx.dose} {rx.frequency}{dur}.")
    if visit.follow_up_days:
        parts.append(f"Follow up in {visit.follow_up_days} days.")
    else:
        parts.append("Follow up as needed.")
    return " ".join(parts)


def visit_to_soap(visit: Visit, patient: Patient) -> str:
    """Render one visit as a SOAP note."""
    return "\n".join(
        [
            f"SOAP NOTE — {visit.visit_date}  ({visit.provider_name})",
            f"S: {_subjective(visit, patient)}",
            f"O: {_objective(visit)}",
            f"A: {_assessment(visit)}",
            f"P: {_plan(visit)}",
        ]
    )


def patient_soap_notes(patient: Patient, last_n: int | None = None) -> list[str]:
    """SOAP notes for a patient's visits (chronological); last_n limits to the most recent."""
    visits = patient.visits[-last_n:] if last_n else patient.visits
    return [visit_to_soap(v, patient) for v in visits]


def polish_with_llm(note: str, model: str = "claude-opus-5") -> str:
    """Rewrite a templated SOAP note as natural clinical prose using Claude."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=(
            "You rewrite templated SOAP notes from a SYNTHETIC dataset as natural, "
            "realistic clinical prose. Keep the S/O/A/P structure, all clinical "
            "values, codes, and dates exactly as given. Output only the note."
        ),
        messages=[{"role": "user", "content": note}],
    )
    if response.stop_reason == "refusal":
        return note
    return next((b.text for b in response.content if b.type == "text"), note)
