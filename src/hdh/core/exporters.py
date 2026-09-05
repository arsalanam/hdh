"""
Exporters: JSON (per-patient), FHIR R4 Bundle, plain-text clinical notes.
"""

import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .models import Patient, Sex

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _date_str(d) -> str:
    if isinstance(d, (date, datetime)):
        return d.isoformat()
    return str(d) if d else ""


def _lab_status_text(status) -> str:
    mapping = {"normal": "Normal", "high": "High (H)", "low": "Low (L)", "critical": "CRITICAL"}
    return mapping.get(str(status).lower(), str(status))


# ─── 1. JSON Exporter ─────────────────────────────────────────────────────────


def patient_to_json(patient: Patient) -> dict:
    """Serialize one patient + full visit history to a JSON-serializable dict."""
    chronic = [
        {
            "icd10": cc.icd10_code,
            "description": cc.description,
            "onset_date": _date_str(cc.onset_date),
            "controlled": cc.controlled,
        }
        for cc in patient.conditions
        if cc.chronic
    ]

    visits_out = []
    for v in patient.visits:
        vital = v.vitals
        vitals_dict = {}
        if vital:
            vitals_dict = {
                "bp": f"{vital.bp_systolic}/{vital.bp_diastolic} mmHg",
                "hr": f"{vital.heart_rate} bpm",
                "rr": f"{vital.respiratory_rate} /min",
                "temp_f": vital.temperature_f,
                "spo2": f"{vital.oxygen_sat}%",
                "weight_kg": vital.weight_kg,
                "height_cm": vital.height_cm,
                "bmi": vital.bmi,
                "pain_scale": vital.pain_scale,
            }

        diagnoses = [
            {"icd10": dx.icd10_code, "description": dx.description, "primary": dx.is_primary}
            for dx in v.conditions
        ]

        prescriptions = [
            {
                "drug": rx.drug_name,
                "class": rx.drug_class,
                "dose": rx.dose,
                "frequency": rx.frequency,
                "duration_days": rx.duration_days,
                "refills": rx.refills,
                "new_rx": rx.is_new,
            }
            for rx in v.prescriptions
        ]

        labs = [
            {
                "test": lr.test_name,
                "value": lr.value,
                "unit": lr.unit,
                "ref_range": f"{lr.reference_low}–{lr.reference_high}",
                "status": str(lr.status).split(".")[-1],
                "loinc": lr.loinc_code,
            }
            for lr in v.lab_results
        ]

        visits_out.append(
            {
                "visit_date": _date_str(v.visit_date),
                "visit_type": str(v.visit_type).split(".")[-1],
                "chief_complaint": v.chief_complaint,
                "provider": v.provider.name if v.provider else None,
                "follow_up_days": v.follow_up_days,
                "vitals": vitals_dict,
                "diagnoses": diagnoses,
                "prescriptions": prescriptions,
                "labs": labs,
            }
        )

    return {
        "patient_id": patient.id,
        "mrn": patient.mrn,
        "name": f"{patient.first_name} {patient.last_name}",
        "dob": _date_str(patient.date_of_birth),
        "age": patient.age,
        "sex": str(patient.sex).split(".")[-1],
        "race": patient.race,
        "ethnicity": patient.ethnicity,
        "address": f"{patient.address}, {patient.city}, {patient.state} {patient.zip_code}",
        "phone": patient.phone,
        "email": patient.email,
        "insurance": patient.insurance_name,
        "blood_type": patient.blood_type,
        "allergies": [
            {
                "substance": a.substance,
                "reaction": a.reaction,
                "severity": str(a.severity).split(".")[-1].lower() if a.severity else None,
            }
            for a in patient.allergies
        ],
        "family_history": [
            {
                "relationship": h.relationship_type,
                "condition": h.condition,
                "icd10_code": h.icd10_code,
                "onset_age": h.onset_age,
            }
            for h in patient.family_history
        ],
        "smoker": patient.smoker,
        "bmi_baseline": patient.bmi_baseline,
        "chronic_conditions": chronic,
        "total_visits": len(visits_out),
        "visits": visits_out,
    }


def export_json(session: Session, output_dir: str = "exports/json", limit: int | None = None):
    """Export each patient to a separate JSON file."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    query = session.query(Patient)
    if limit:
        query = query.limit(limit)
    patients = query.all()

    for p in patients:
        data = patient_to_json(p)
        path = out / f"patient_{p.mrn}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"✅ JSON export: {len(patients)} files → {out}/")
    return len(patients)


def export_json_bulk(
    session: Session, output_file: str = "exports/all_patients.json", limit: int | None = None
):
    """Export all patients to a single JSON array file."""
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    query = session.query(Patient)
    if limit:
        query = query.limit(limit)
    patients = query.all()
    data = [patient_to_json(p) for p in patients]
    Path(output_file).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"✅ Bulk JSON export: {len(patients)} patients → {output_file}")
    return len(patients)


# ─── 2. FHIR R4 Exporter ─────────────────────────────────────────────────────


def patient_to_fhir_bundle(patient: Patient) -> dict:
    """Generate a FHIR R4 Bundle for one patient.

    Assembly lives in hdh.core.fhir: pluggable per-resource emitters plus
    module-contributed enrichers (design docs/design/fhir-emitters.md).
    """
    from hdh.core.fhir import build_bundle

    return build_bundle(patient)


def export_fhir(session: Session, output_dir: str = "exports/fhir", limit: int | None = None):
    """Export each patient as a FHIR R4 Bundle JSON file."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    query = session.query(Patient)
    if limit:
        query = query.limit(limit)
    patients = query.all()

    for p in patients:
        bundle = patient_to_fhir_bundle(p)
        path = out / f"fhir_bundle_{p.mrn}.json"
        path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    print(f"✅ FHIR R4 export: {len(patients)} bundles → {out}/")
    return len(patients)


# ─── 3. Plain-Text Clinical Notes Exporter ────────────────────────────────────


def _allergy_line(patient) -> str:
    """Allergies, with what each one actually did.

    The severity and the reaction were in the row and not on the page: a
    chart reading `Allergies: Aspirin` is the same string whether that was
    a mild stomach upset or anaphylaxis, and the agent prescribing next
    cannot tell. Both are stored; both are now rendered.

    `NKDA` for an empty list is the chart's contract rather than a guess —
    see :class:`hdh.core.models.Allergy`. It is stated as *no known
    allergies recorded* so a reader can see it came from an empty list, not
    from someone having asked and written it down.
    """
    live = [a for a in patient.allergies if getattr(a, "voided_at", None) is None]
    if not live:
        return "none recorded (NKDA)"
    parts = []
    for allergy in live:
        detail = ", ".join(
            bit
            for bit in (
                getattr(allergy.severity, "value", None) or None,
                allergy.reaction or None,
            )
            if bit
        )
        coded = f" [{allergy.code_standard}:{allergy.drug_code}]" if allergy.drug_code else ""
        parts.append(f"{allergy.substance}{coded}" + (f" ({detail})" if detail else ""))
    return "; ".join(parts)


def _append_immunizations(lines: list, patient) -> None:
    """Immunisations, which the chart generated and never showed.

    Zero mentions in this function before now, while the rows sat in the
    database — so an agent asked what a patient had been given answered from
    an absence it had no way to recognise as one.
    """
    shots = sorted(
        patient.immunizations,
        key=lambda i: (i.administered_date is None, i.administered_date),
        reverse=True,
    )
    if not shots:
        return
    lines += ["IMMUNISATIONS", "-" * 40]
    for shot in shots:
        dose = f"  dose {shot.dose_number}" if shot.dose_number else ""
        code = f"  [CVX {shot.cvx_code}]" if shot.cvx_code else ""
        lines.append(f"  {_date_str(shot.administered_date)}  {shot.vaccine}{dose}{code}")
    lines.append("")


def _append_procedures(lines: list, patient) -> None:
    """Procedures, including those with no visit — a past appendectomy has
    no encounter in this practice, and `procedures.visit_id` has always been
    nullable precisely so it can be recorded anyway."""
    done = sorted(
        patient.procedures,
        key=lambda p: (p.performed_date is None, p.performed_date),
        reverse=True,
    )
    if not done:
        return
    lines += ["PROCEDURES", "-" * 40]
    for item in done:
        history = "" if item.visit_id else "  (history — not performed here)"
        lines.append(f"  {_date_str(item.performed_date)}  {item.description}{history}")
    lines.append("")


def patient_to_text(patient: Patient) -> str:
    """Produce a plain-text chart summary readable by an LLM."""
    lines = []

    # `Sex.label`, not a substring test: "MALE" is a substring of "FEMALE",
    # so the old expression rendered every patient as Male — including in
    # the chart text the agent reads and reasons over.
    # `Sex.coerce`, because `patient.sex` is not reliably the enum: a
    # freshly built instance carries `Sex.FEMALE`, one reloaded after a
    # flush carries the raw column value `'F'`, and `.label` on the second
    # raises. The chart the agent reads crashed outright on that path.
    resolved_sex = Sex.coerce(patient.sex)
    sex_label = resolved_sex.label if resolved_sex is not None else "Unknown"
    lines += [
        "=" * 70,
        "PATIENT CHART SUMMARY",
        "=" * 70,
        f"MRN          : {patient.mrn}",
        f"Name         : {patient.first_name} {patient.last_name}",
        f"DOB          : {_date_str(patient.date_of_birth)}  |  Age: {patient.age}  |  Sex: {sex_label}",
        f"Blood Type   : {patient.blood_type}",
        f"Insurance    : {patient.insurance_name}  (ID: {patient.insurance_id})",
        "Allergies    : " + _allergy_line(patient),
        "",
        "FAMILY HISTORY",
        "-" * 40,
    ]
    fh_items = [f"{h.relationship_type}: {h.condition}" for h in patient.family_history]
    lines.append("; ".join(fh_items) if fh_items else "None reported")
    lines.append(f"Smoker       : {'Yes' if patient.smoker else 'No'}")
    lines.append(f"BMI (baseline): {patient.bmi_baseline}")
    lines.append("")

    _append_immunizations(lines, patient)
    _append_procedures(lines, patient)

    if any(c.chronic for c in patient.conditions):
        lines += ["ACTIVE CHRONIC CONDITIONS", "-" * 40]
        for cc in (c for c in patient.conditions if c.chronic):
            controlled = "Controlled" if cc.controlled else "Uncontrolled"
            lines.append(
                f"  [{cc.icd10_code}] {cc.description}  —  Onset: {_date_str(cc.onset_date)}  ({controlled})"
            )
        lines.append("")

    lines += [
        f"VISIT HISTORY  ({len(patient.visits)} total visits)",
        "=" * 70,
    ]

    for v in patient.visits:
        vtype = str(v.visit_type).split(".")[-1].replace("_", " ").title()
        lines += [
            "",
            f"DATE: {_date_str(v.visit_date)}  [{vtype}]  —  Provider: "
            + (v.provider.name if v.provider else "Unassigned"),
            f"CHIEF COMPLAINT: {v.chief_complaint}",
        ]

        # Vitals
        if v.vitals:
            vt = v.vitals
            lines.append(
                f"VITALS: BP {vt.bp_systolic}/{vt.bp_diastolic} mmHg  |  "
                f"HR {vt.heart_rate} bpm  |  RR {vt.respiratory_rate}/min  |  "
                f"Temp {vt.temperature_f}°F  |  SpO2 {vt.oxygen_sat}%  |  "
                f"Wt {vt.weight_kg}kg  |  BMI {vt.bmi}  |  Pain {vt.pain_scale}/10"
            )

        # Diagnoses
        if v.conditions:
            dx_str = "; ".join(f"{dx.icd10_code} – {dx.description}" for dx in v.conditions)
            lines.append(f"ASSESSMENT: {dx_str}")

        # Prescriptions
        if v.prescriptions:
            for rx in v.prescriptions:
                status = "New" if rx.is_new else "Refill"
                dur = f"{rx.duration_days}d" if rx.duration_days else "Ongoing"
                lines.append(
                    f"  Rx [{status}]: {rx.drug_name} {rx.dose} {rx.frequency}  ×{dur}  Refills: {rx.refills}"
                )

        # Labs
        if v.lab_results:
            lines.append("  LABS:")
            for lr in v.lab_results:
                flag = " ◄" if str(lr.status).split(".")[-1].lower() != "normal" else ""
                lines.append(
                    f"    {lr.test_name:<30} {lr.value:>8.2f} {lr.unit:<10} "
                    f"(Ref: {lr.reference_low}–{lr.reference_high})  "
                    f"{_lab_status_text(lr.status)}{flag}"
                )

        if v.follow_up_days:
            lines.append(f"FOLLOW-UP: Return in {v.follow_up_days} days")
        else:
            lines.append("FOLLOW-UP: PRN / as needed")

        lines.append("-" * 70)

    return "\n".join(lines)


def export_text(session: Session, output_dir: str = "exports/text", limit: int | None = None):
    """Export each patient as a plain-text .txt file."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    query = session.query(Patient)
    if limit:
        query = query.limit(limit)
    patients = query.all()

    for p in patients:
        text = patient_to_text(p)
        path = out / f"chart_{p.mrn}.txt"
        path.write_text(text, encoding="utf-8")

    print(f"✅ Text export: {len(patients)} charts → {out}/")
    return len(patients)
