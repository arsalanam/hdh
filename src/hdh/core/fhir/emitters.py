"""Core FHIR emitters — one small class per resource type.

Ported emitters reproduce the pre-refactor field shapes exactly (the
golden-bundle test holds them to it); new emitters cover the v0.4.0 chart
entities. Coding enrichment (SNOMED etc.) belongs to module enrichers,
never here.
"""

from __future__ import annotations

import base64
from typing import ClassVar

from hdh.core.fhir import _ENTITY_KEY, ExportContext
from hdh.core.fhir.terminology import (
    BP_COMPONENTS,
    BP_PANEL,
    CONDITION_CLINICAL_STATUS,
    ENCOUNTER_CLASS,
    SYSTEMS,
    VITALS_PANEL,
)


def _d(value) -> str:
    return value.isoformat() if value else ""


class PatientEmitter:
    """The Patient resource (id stays the MRN — the chart's anchor)."""

    resource_type: ClassVar[str] = "Patient"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        p = ctx.patient
        return [
            {
                "resourceType": "Patient",
                "id": p.mrn,
                "identifier": [{"system": SYSTEMS["mrn"], "value": p.mrn}],
                "name": [{"use": "official", "family": p.last_name, "given": [p.first_name]}],
                "gender": "female" if str(p.sex).endswith("FEMALE") else "male",
                "birthDate": _d(p.date_of_birth),
                "address": [
                    {"line": [p.address], "city": p.city, "state": p.state, "postalCode": p.zip_code}
                ],
                "telecom": [{"system": "phone", "value": p.phone}],
            }
        ]


class PractitionerEmitter:
    """Thin Practitioner rows for every provider this chart references."""

    resource_type: ClassVar[str] = "Practitioner"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        seen: dict[int, dict] = {}
        for visit in ctx.patient.visits:
            prov = visit.provider
            if prov and prov.id not in seen:
                seen[prov.id] = {
                    "resourceType": "Practitioner",
                    "id": ctx.rid("Practitioner", prov.identifier),
                    "identifier": [{"value": prov.identifier}],
                    "name": [{"text": prov.name}],
                }
        return list(seen.values())


class EncounterEmitter:
    """One Encounter per visit (ids derivable via ctx.encounter_ref)."""

    resource_type: ClassVar[str] = "Encounter"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for v in ctx.patient.visits:
            vtype = str(v.visit_type).split(".")[-1].lower()
            out.append(
                {
                    "resourceType": "Encounter",
                    "id": ctx.rid("Encounter", v.id),
                    "status": "finished",
                    "class": {"code": ENCOUNTER_CLASS.get(vtype, "AMB")},
                    "subject": {"reference": f"Patient/{ctx.mrn}"},
                    "period": {"start": _d(v.visit_date), "end": _d(v.visit_date)},
                    "reasonCode": [{"text": v.chief_complaint}],
                    "participant": [
                        {"individual": {"display": v.provider.name if v.provider else "Unassigned"}}
                    ],
                    _ENTITY_KEY: v,
                }
            )
        return out


class ConditionEmitter:
    """Coded Conditions (ICD-10 here; other codings arrive via enrichers)."""

    resource_type: ClassVar[str] = "Condition"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for cond in ctx.patient.conditions:
            status = CONDITION_CLINICAL_STATUS.get(str(cond.status).split(".")[-1], "active")
            resource = {
                "resourceType": "Condition",
                "id": ctx.rid("Condition", cond.icd10_code, cond.onset_date, cond.chronic),
                "clinicalStatus": {"coding": [{"code": status}]},
                "code": {
                    "coding": [
                        {
                            "system": SYSTEMS["icd10"],
                            "code": cond.icd10_code,
                            "display": cond.description,
                        }
                    ]
                },
                "subject": {"reference": f"Patient/{ctx.mrn}"},
                "recordedDate": _d(cond.onset_date or (cond.visit.visit_date if cond.visit else None)),
                _ENTITY_KEY: cond,
            }
            if cond.visit is not None:
                resource["encounter"] = ctx.encounter_ref(cond.visit)
            out.append(resource)
        return out


class VitalsObservationEmitter:
    """LOINC-coded vitals panel per visit."""

    resource_type: ClassVar[str] = "Observation"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for v in ctx.patient.visits:
            vt = v.vitals
            if not vt:
                continue
            bp_code, bp_display = BP_PANEL
            out.append(
                {
                    "resourceType": "Observation",
                    "id": ctx.rid("Observation", v.id, bp_code),
                    "status": "final",
                    "code": {
                        "coding": [{"system": SYSTEMS["loinc"], "code": bp_code, "display": bp_display}]
                    },
                    "subject": {"reference": f"Patient/{ctx.mrn}"},
                    "encounter": ctx.encounter_ref(v),
                    "effectiveDateTime": _d(v.visit_date),
                    "component": [
                        {
                            "code": {"coding": [{"system": SYSTEMS["loinc"], "code": c, "display": d}]},
                            "valueQuantity": {"value": getattr(vt, attr), "unit": "mm[Hg]"},
                        }
                        for c, d, attr in BP_COMPONENTS
                    ],
                    _ENTITY_KEY: vt,
                }
            )
            for loinc, display, attr, unit in VITALS_PANEL:
                out.append(
                    {
                        "resourceType": "Observation",
                        "id": ctx.rid("Observation", v.id, loinc),
                        "status": "final",
                        "code": {"coding": [{"system": SYSTEMS["loinc"], "code": loinc, "display": display}]},
                        "subject": {"reference": f"Patient/{ctx.mrn}"},
                        "encounter": ctx.encounter_ref(v),
                        "effectiveDateTime": _d(v.visit_date),
                        "valueQuantity": {"value": getattr(vt, attr), "unit": unit},
                        _ENTITY_KEY: vt,
                    }
                )
        return out


class LabObservationEmitter:
    """LOINC-coded lab Observations with ranges and interpretation."""

    resource_type: ClassVar[str] = "Observation"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for v in ctx.patient.visits:
            for seq, lr in enumerate(v.lab_results):
                out.append(
                    {
                        "resourceType": "Observation",
                        "id": ctx.rid("Observation", v.id, "lab", lr.loinc_code, seq),
                        "status": "final",
                        "category": [{"coding": [{"system": SYSTEMS["obs-category"], "code": "laboratory"}]}],
                        "code": {
                            "coding": [
                                {"system": SYSTEMS["loinc"], "code": lr.loinc_code, "display": lr.test_name}
                            ]
                        },
                        "subject": {"reference": f"Patient/{ctx.mrn}"},
                        "encounter": ctx.encounter_ref(v),
                        "effectiveDateTime": _d(v.visit_date),
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
                                        "system": SYSTEMS["interpretation"],
                                        "code": str(lr.status).split(".")[-1].upper()[0],
                                    }
                                ]
                            }
                        ],
                        _ENTITY_KEY: lr,
                    }
                )
        return out


class MedicationRequestEmitter:
    """MedicationRequest per prescription (the order events)."""

    resource_type: ClassVar[str] = "MedicationRequest"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for v in ctx.patient.visits:
            for seq, rx in enumerate(v.prescriptions):
                out.append(
                    {
                        "resourceType": "MedicationRequest",
                        "id": ctx.rid("MedicationRequest", v.id, rx.drug_name, seq),
                        "status": "active",
                        "intent": "order",
                        "medicationCodeableConcept": {"text": f"{rx.drug_name} {rx.dose}"},
                        "subject": {"reference": f"Patient/{ctx.mrn}"},
                        "encounter": ctx.encounter_ref(v),
                        "authoredOn": _d(v.visit_date),
                        "dosageInstruction": [
                            {"text": f"{rx.dose} {rx.frequency}", "timing": {"repeat": {"frequency": 1}}}
                        ],
                        "dispenseRequest": {"numberOfRepeatsAllowed": rx.refills},
                        "note": [{"text": rx.drug_class}],
                        _ENTITY_KEY: rx,
                    }
                )
        return out


class AllergyIntoleranceEmitter:
    """Structured allergies (v0.4.0)."""

    resource_type: ClassVar[str] = "AllergyIntolerance"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for a in ctx.patient.allergies:
            severity = str(a.severity).split(".")[-1].lower() if a.severity else None
            resource = {
                "resourceType": "AllergyIntolerance",
                "id": ctx.rid("AllergyIntolerance", a.substance),
                "clinicalStatus": {"coding": [{"system": SYSTEMS["allergy-clinical"], "code": "active"}]},
                "code": {"text": a.substance},
                "patient": {"reference": f"Patient/{ctx.mrn}"},
                "recordedDate": _d(a.noted_date),
                _ENTITY_KEY: a,
            }
            if a.reaction:
                reaction: dict = {"manifestation": [{"text": a.reaction}]}
                if severity:
                    reaction["severity"] = severity
                resource["reaction"] = [reaction]
            out.append(resource)
        return out


class FamilyMemberHistoryEmitter:
    """Structured family history + lightweight relatives' narrative notes."""

    resource_type: ClassVar[str] = "FamilyMemberHistory"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for h in ctx.patient.family_history:
            resource = {
                "resourceType": "FamilyMemberHistory",
                "id": ctx.rid("FamilyMemberHistory", h.relationship_type, h.condition),
                "status": "completed",
                "patient": {"reference": f"Patient/{ctx.mrn}"},
                "relationship": {"text": h.relationship_type},
                "condition": [
                    {
                        "code": {
                            "coding": (
                                [{"system": SYSTEMS["icd10"], "code": h.icd10_code, "display": h.condition}]
                                if h.icd10_code
                                else []
                            ),
                            "text": h.condition,
                        },
                        **(
                            {"onsetAge": {"value": h.onset_age, "unit": "a"}}
                            if h.onset_age is not None
                            else {}
                        ),
                    }
                ],
                _ENTITY_KEY: h,
            }
            member = h.family_member
            if member is not None and member.summary:
                resource["note"] = [{"text": member.summary}]
            out.append(resource)
        return out


class MedicationStatementEmitter:
    """The cross-visit medication list (v0.4.0)."""

    resource_type: ClassVar[str] = "MedicationStatement"

    STATUS = {"ACTIVE": "active", "COMPLETED": "completed", "STOPPED": "stopped"}

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for m in ctx.patient.medications:
            resource = {
                "resourceType": "MedicationStatement",
                "id": ctx.rid("MedicationStatement", m.drug_name, m.start_date),
                "status": self.STATUS.get(str(m.status).split(".")[-1], "active"),
                "medicationCodeableConcept": {"text": f"{m.drug_name} {m.dose or ''}".strip()},
                "subject": {"reference": f"Patient/{ctx.mrn}"},
                "effectivePeriod": {
                    "start": _d(m.start_date),
                    **({"end": _d(m.end_date)} if m.end_date else {}),
                },
                "dosage": [{"text": f"{m.dose or ''} {m.frequency or ''}".strip()}],
                _ENTITY_KEY: m,
            }
            if m.indication is not None:
                resource["reasonCode"] = [{"text": m.indication.description}]
            out.append(resource)
        return out


class ProcedureEmitter:
    """Performed procedures (v0.4.0) — the SNOMED intervention slot."""

    resource_type: ClassVar[str] = "Procedure"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for proc in ctx.patient.procedures:
            resource = {
                "resourceType": "Procedure",
                "id": ctx.rid("Procedure", proc.description, proc.performed_date),
                "status": "completed",
                "code": {"text": proc.description},
                "subject": {"reference": f"Patient/{ctx.mrn}"},
                "performedDateTime": _d(proc.performed_date),
                _ENTITY_KEY: proc,
            }
            if proc.visit is not None:
                resource["encounter"] = ctx.encounter_ref(proc.visit)
            out.append(resource)
        return out


class ImmunizationEmitter:
    """CVX-coded immunization history (v0.4.0)."""

    resource_type: ClassVar[str] = "Immunization"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for imm in ctx.patient.immunizations:
            resource = {
                "resourceType": "Immunization",
                "id": ctx.rid("Immunization", imm.vaccine, imm.administered_date, imm.dose_number),
                "status": "completed",
                "vaccineCode": {
                    "coding": (
                        [{"system": SYSTEMS["cvx"], "code": imm.cvx_code, "display": imm.vaccine}]
                        if imm.cvx_code
                        else []
                    ),
                    "text": imm.vaccine,
                },
                "patient": {"reference": f"Patient/{ctx.mrn}"},
                "occurrenceDateTime": _d(imm.administered_date),
                _ENTITY_KEY: imm,
            }
            if imm.dose_number is not None:
                resource["protocolApplied"] = [{"doseNumberPositiveInt": imm.dose_number}]
            out.append(resource)
        return out


class DocumentReferenceEmitter:
    """Stored visit notes as DocumentReference (review decision Q2 — the
    comprehension service's Composition comes later, from its module)."""

    resource_type: ClassVar[str] = "DocumentReference"

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Build this type's resources from the chart (see class doc)."""
        out = []
        for v in ctx.patient.visits:
            for note in v.notes:
                out.append(
                    {
                        "resourceType": "DocumentReference",
                        "id": ctx.rid("DocumentReference", v.id, note.note_type),
                        "status": "current",
                        "type": {"text": f"{str(note.note_type).split('.')[-1]} note"},
                        "subject": {"reference": f"Patient/{ctx.mrn}"},
                        # FHIR instant requires time + zone (conformance-gate catch)
                        "date": f"{_d(v.visit_date)}T00:00:00Z",
                        "context": {"encounter": [ctx.encounter_ref(v)]},
                        "content": [
                            {
                                "attachment": {
                                    "contentType": "text/plain",
                                    "data": base64.b64encode(note.text.encode()).decode(),
                                }
                            }
                        ],
                        _ENTITY_KEY: note,
                    }
                )
        return out
