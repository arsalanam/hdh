"""
Exporters: JSON (per-patient), FHIR R4 Bundle, plain-text clinical notes.
"""

import json
import uuid
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .models import Patient

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
        for cc in patient.chronic_conditions
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
            for dx in v.diagnoses
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
                "provider": v.provider_name,
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
        "allergies": patient.allergies.split("|") if patient.allergies else [],
        "family_history": {
            "diabetes": patient.fam_hx_diabetes,
            "hypertension": patient.fam_hx_hypertension,
            "heart_disease": patient.fam_hx_heart_disease,
            "cancer": patient.fam_hx_cancer,
        },
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


def _fhir_patient_resource(patient: Patient) -> dict:
    """The FHIR Patient resource for one patient."""
    return {
        "resourceType": "Patient",
        "id": patient.mrn,
        "identifier": [{"system": "urn:family-medicine-mrn", "value": patient.mrn}],
        "name": [{"use": "official", "family": patient.last_name, "given": [patient.first_name]}],
        "gender": "male" if str(patient.sex).endswith("MALE") or str(patient.sex).endswith("M") else "female",
        "birthDate": _date_str(patient.date_of_birth),
        "address": [
            {
                "line": [patient.address],
                "city": patient.city,
                "state": patient.state,
                "postalCode": patient.zip_code,
            }
        ],
        "telecom": [{"system": "phone", "value": patient.phone}],
    }


def _fhir_encounter(v, mrn: str, enc_id: str) -> dict:
    """The FHIR Encounter resource for one visit."""
    visit_type_map = {"acute": "AMB", "follow_up": "AMB", "preventive": "AMB", "urgent": "EMER"}
    vtype = str(v.visit_type).split(".")[-1]
    return {
        "resourceType": "Encounter",
        "id": enc_id,
        "status": "finished",
        "class": {"code": visit_type_map.get(vtype, "AMB")},
        "subject": {"reference": f"Patient/{mrn}"},
        "period": {"start": _date_str(v.visit_date), "end": _date_str(v.visit_date)},
        "reasonCode": [{"text": v.chief_complaint}],
        "participant": [{"individual": {"display": v.provider_name}}],
    }


def _fhir_vitals_observations(v, mrn: str, enc_id: str) -> list[dict]:
    """LOINC-coded Observation resources for a visit's vitals panel."""
    if not v.vitals:
        return []
    vt = v.vitals
    observations = [
        ("55284-4", "BP", f"{vt.bp_systolic}/{vt.bp_diastolic}", "mm[Hg]"),
        ("8867-4", "Heart rate", vt.heart_rate, "/min"),
        ("9279-1", "Respiratory rate", vt.respiratory_rate, "/min"),
        ("8310-5", "Body temperature", vt.temperature_f, "degF"),
        ("59408-5", "SpO2", vt.oxygen_sat, "%"),
        ("29463-7", "Body weight", vt.weight_kg, "kg"),
        ("8302-2", "Body height", vt.height_cm, "cm"),
        ("39156-5", "BMI", vt.bmi, "kg/m2"),
    ]
    return [
        {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
            "subject": {"reference": f"Patient/{mrn}"},
            "encounter": {"reference": f"Encounter/{enc_id}"},
            "effectiveDateTime": _date_str(v.visit_date),
            "valueQuantity": {"value": val, "unit": unit},
        }
        for loinc, display, val, unit in observations
    ]


def _fhir_conditions(v, mrn: str, enc_id: str) -> list[dict]:
    """ICD-10-coded Condition resources for a visit's diagnoses."""
    return [
        {
            "resourceType": "Condition",
            "id": str(uuid.uuid4()),
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10",
                        "code": dx.icd10_code,
                        "display": dx.description,
                    }
                ]
            },
            "subject": {"reference": f"Patient/{mrn}"},
            "encounter": {"reference": f"Encounter/{enc_id}"},
            "recordedDate": _date_str(v.visit_date),
        }
        for dx in v.diagnoses
    ]


def _fhir_medication_requests(v, mrn: str, enc_id: str) -> list[dict]:
    """MedicationRequest resources for a visit's prescriptions."""
    return [
        {
            "resourceType": "MedicationRequest",
            "id": str(uuid.uuid4()),
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {"text": f"{rx.drug_name} {rx.dose}"},
            "subject": {"reference": f"Patient/{mrn}"},
            "encounter": {"reference": f"Encounter/{enc_id}"},
            "authoredOn": _date_str(v.visit_date),
            "dosageInstruction": [
                {
                    "text": f"{rx.dose} {rx.frequency}",
                    "timing": {"repeat": {"frequency": 1}},
                }
            ],
            "dispenseRequest": {"numberOfRepeatsAllowed": rx.refills},
            "note": [{"text": rx.drug_class}],
        }
        for rx in v.prescriptions
    ]


def _fhir_lab_observations(v, mrn: str, enc_id: str) -> list[dict]:
    """LOINC-coded Observation resources for a visit's lab results."""
    return [
        {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory",
                        }
                    ]
                }
            ],
            "code": {
                "coding": [{"system": "http://loinc.org", "code": lr.loinc_code, "display": lr.test_name}]
            },
            "subject": {"reference": f"Patient/{mrn}"},
            "encounter": {"reference": f"Encounter/{enc_id}"},
            "effectiveDateTime": _date_str(v.visit_date),
            "valueQuantity": {"value": lr.value, "unit": lr.unit},
            "referenceRange": [
                {
                    "low": {"value": lr.reference_low, "unit": lr.unit},
                    "high": {"value": lr.reference_high, "unit": lr.unit},
                }
            ],
            "interpretation": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                            "code": str(lr.status).split(".")[-1].upper()[0],
                        }
                    ]
                }
            ],
        }
        for lr in v.lab_results
    ]


def patient_to_fhir_bundle(patient: Patient) -> dict:
    """Generate a FHIR R4 Bundle for one patient: Patient, then per visit an
    Encounter with its Observations, Conditions, and MedicationRequests."""
    entries = [{"resource": _fhir_patient_resource(patient)}]
    for v in patient.visits:
        enc_id = str(uuid.uuid4())
        resources = (
            [_fhir_encounter(v, patient.mrn, enc_id)]
            + _fhir_vitals_observations(v, patient.mrn, enc_id)
            + _fhir_conditions(v, patient.mrn, enc_id)
            + _fhir_medication_requests(v, patient.mrn, enc_id)
            + _fhir_lab_observations(v, patient.mrn, enc_id)
        )
        entries.extend({"resource": r} for r in resources)

    return {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "type": "collection",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "total": len(entries),
        "entry": entries,
    }


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


def patient_to_text(patient: Patient) -> str:
    """Produce a plain-text chart summary readable by an LLM."""
    lines = []

    sex_label = "Male" if str(patient.sex).endswith("M") or "MALE" in str(patient.sex) else "Female"
    lines += [
        "=" * 70,
        "PATIENT CHART SUMMARY",
        "=" * 70,
        f"MRN          : {patient.mrn}",
        f"Name         : {patient.first_name} {patient.last_name}",
        f"DOB          : {_date_str(patient.date_of_birth)}  |  Age: {patient.age}  |  Sex: {sex_label}",
        f"Blood Type   : {patient.blood_type}",
        f"Insurance    : {patient.insurance_name}  (ID: {patient.insurance_id})",
        f"Allergies    : {patient.allergies}",
        "",
        "FAMILY HISTORY",
        "-" * 40,
    ]
    fh_items = []
    if patient.fam_hx_diabetes:
        fh_items.append("Type 2 Diabetes")
    if patient.fam_hx_hypertension:
        fh_items.append("Hypertension")
    if patient.fam_hx_heart_disease:
        fh_items.append("Heart Disease")
    if patient.fam_hx_cancer:
        fh_items.append("Cancer")
    lines.append(", ".join(fh_items) if fh_items else "None reported")
    lines.append(f"Smoker       : {'Yes' if patient.smoker else 'No'}")
    lines.append(f"BMI (baseline): {patient.bmi_baseline}")
    lines.append("")

    if patient.chronic_conditions:
        lines += ["ACTIVE CHRONIC CONDITIONS", "-" * 40]
        for cc in patient.chronic_conditions:
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
            f"DATE: {_date_str(v.visit_date)}  [{vtype}]  —  Provider: {v.provider_name}",
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
        if v.diagnoses:
            dx_str = "; ".join(f"{dx.icd10_code} – {dx.description}" for dx in v.diagnoses)
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
