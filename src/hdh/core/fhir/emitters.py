"""Core FHIR emitters — typed construction, one small class per resource.

Every emitter builds official ``fhir.resources`` R4B models, nested
datatypes included: a wrong field name, a string in a decimal, or a
missing required field fails at the construction line — statically under
mypy and at runtime under pydantic. That is the best-practice contract
for human and AI authors alike (design fhir-emitters.md §6, adopted
2026-08-13).

Coding enrichment (SNOMED etc.) belongs to module enrichers, never here.
"""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any, ClassVar

from fhir.resources.R4B.address import Address
from fhir.resources.R4B.age import Age
from fhir.resources.R4B.allergyintolerance import AllergyIntolerance, AllergyIntoleranceReaction
from fhir.resources.R4B.annotation import Annotation
from fhir.resources.R4B.attachment import Attachment
from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.condition import Condition
from fhir.resources.R4B.contactpoint import ContactPoint
from fhir.resources.R4B.documentreference import (
    DocumentReference,
    DocumentReferenceContent,
    DocumentReferenceContext,
)
from fhir.resources.R4B.dosage import Dosage
from fhir.resources.R4B.encounter import Encounter, EncounterParticipant
from fhir.resources.R4B.familymemberhistory import (
    FamilyMemberHistory,
    FamilyMemberHistoryCondition,
)
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.immunization import Immunization, ImmunizationProtocolApplied
from fhir.resources.R4B.medicationrequest import (
    MedicationRequest,
    MedicationRequestDispenseRequest,
)
from fhir.resources.R4B.medicationstatement import MedicationStatement
from fhir.resources.R4B.observation import (
    Observation,
    ObservationComponent,
    ObservationReferenceRange,
)
from fhir.resources.R4B.patient import Patient as FhirPatient
from fhir.resources.R4B.period import Period
from fhir.resources.R4B.practitioner import Practitioner
from fhir.resources.R4B.procedure import Procedure as FhirProcedure
from fhir.resources.R4B.quantity import Quantity
from fhir.resources.R4B.reference import Reference
from fhir.resources.R4B.timing import Timing, TimingRepeat

from hdh.core.fhir import ExportContext
from hdh.core.fhir.terminology import (
    BP_COMPONENTS,
    BP_PANEL,
    CONDITION_CLINICAL_STATUS,
    ENCOUNTER_CLASS,
    SYSTEMS,
    VITALS_PANEL,
)


def _d(value):
    """FHIR date string, or None (typed models rightly reject "").

    Deliberately unannotated: pydantic validates the string at runtime,
    and the implicit Any keeps mypy from demanding date objects where
    FHIR's partial-precision dates are broader than ``datetime.date``.
    """
    return value.isoformat() if value else None


def _instant(value):
    """Date-only source as a FHIR *instant* (midnight UTC), or None."""
    return f"{value.isoformat()}T00:00:00Z" if value else None


def _qty(value: Any, unit: str | None) -> Quantity:
    """FHIR Quantity — through ``str`` so floats keep their printed repr."""
    return Quantity(value=None if value is None else Decimal(str(value)), unit=unit)


def _loinc(code: str | None, display: str) -> CodeableConcept:
    return CodeableConcept(coding=[Coding(system=SYSTEMS["loinc"], code=code, display=display)])


def _patient_ref(ctx: ExportContext) -> Reference:
    return Reference(reference=f"Patient/{ctx.mrn}")


def _encounter_ref(ctx: ExportContext, visit) -> Reference:
    return Reference(**ctx.encounter_ref(visit))


class PatientEmitter:
    """The Patient resource (id stays the MRN — the chart's anchor)."""

    resource_type: ClassVar[str] = "Patient"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build the Patient resource (typed)."""
        p = ctx.patient
        return [
            (
                FhirPatient(
                    id=p.mrn,
                    identifier=[Identifier(system=SYSTEMS["mrn"], value=p.mrn)],
                    name=[HumanName(use="official", family=p.last_name, given=[p.first_name])],
                    gender="female" if str(p.sex).endswith("FEMALE") else "male",
                    birthDate=_d(p.date_of_birth),
                    address=[Address(line=[p.address], city=p.city, state=p.state, postalCode=p.zip_code)],
                    telecom=[ContactPoint(system="phone", value=p.phone)],
                ),
                p,
            )
        ]


class PractitionerEmitter:
    """Thin Practitioner rows for every provider this chart references."""

    resource_type: ClassVar[str] = "Practitioner"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build Practitioner resources (typed, deduplicated)."""
        seen: dict[int, tuple[Any, Any]] = {}
        for visit in ctx.patient.visits:
            prov = visit.provider
            if prov and prov.id not in seen:
                seen[prov.id] = (
                    Practitioner(
                        id=ctx.rid("Practitioner", prov.identifier),
                        identifier=[Identifier(value=prov.identifier)],
                        name=[HumanName(text=prov.name)],
                    ),
                    prov,
                )
        return list(seen.values())


class EncounterEmitter:
    """One Encounter per visit (ids derivable via ctx.encounter_ref)."""

    resource_type: ClassVar[str] = "Encounter"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build Encounter resources (typed)."""
        out = []
        for v in ctx.patient.visits:
            vtype = str(v.visit_type).split(".")[-1].lower()
            # "class" is a Python keyword; the model's field is class_fhir
            # with alias "class" — unpacking sidesteps the alias-only init.
            encounter_class: dict[str, Any] = {"class": Coding(code=ENCOUNTER_CLASS.get(vtype, "AMB"))}
            out.append(
                (
                    Encounter(
                        id=ctx.rid("Encounter", v.id),
                        status="finished",
                        subject=_patient_ref(ctx),
                        period=Period(start=_d(v.visit_date), end=_d(v.visit_date)),
                        reasonCode=[CodeableConcept(text=v.chief_complaint)],
                        participant=[
                            EncounterParticipant(
                                individual=Reference(display=v.provider.name if v.provider else "Unassigned")
                            )
                        ],
                        **encounter_class,
                    ),
                    v,
                )
            )
        return out


class ConditionEmitter:
    """Coded Conditions (ICD-10 here; other codings arrive via enrichers)."""

    resource_type: ClassVar[str] = "Condition"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build Condition resources (typed) from the unified problem list."""
        out = []
        for cond in ctx.patient.conditions:
            status = CONDITION_CLINICAL_STATUS.get(str(cond.status).split(".")[-1], "active")
            resource = Condition(
                id=ctx.rid("Condition", cond.icd10_code, cond.onset_date, cond.chronic),
                clinicalStatus=CodeableConcept(coding=[Coding(code=status)]),
                code=CodeableConcept(
                    coding=[Coding(system=SYSTEMS["icd10"], code=cond.icd10_code, display=cond.description)]
                ),
                subject=_patient_ref(ctx),
                recordedDate=_d(cond.onset_date or (cond.visit.visit_date if cond.visit else None)),
            )
            if cond.visit is not None:
                resource.encounter = _encounter_ref(ctx, cond.visit)
            out.append((resource, cond))
        return out


class VitalsObservationEmitter:
    """LOINC-coded vitals panel per visit (BP as components — FHIR decimals)."""

    resource_type: ClassVar[str] = "Observation"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build vitals Observations (typed); BP is component-based."""
        out = []
        for v in ctx.patient.visits:
            vt = v.vitals
            if not vt:
                continue
            bp_code, bp_display = BP_PANEL
            out.append(
                (
                    Observation(
                        id=ctx.rid("Observation", v.id, bp_code),
                        status="final",
                        code=_loinc(bp_code, bp_display),
                        subject=_patient_ref(ctx),
                        encounter=_encounter_ref(ctx, v),
                        effectiveDateTime=_d(v.visit_date),
                        component=[
                            ObservationComponent(
                                code=_loinc(c, d),
                                valueQuantity=_qty(getattr(vt, attr), "mm[Hg]"),
                            )
                            for c, d, attr in BP_COMPONENTS
                        ],
                    ),
                    vt,
                )
            )
            for loinc, display, attr, unit in VITALS_PANEL:
                out.append(
                    (
                        Observation(
                            id=ctx.rid("Observation", v.id, loinc),
                            status="final",
                            code=_loinc(loinc, display),
                            subject=_patient_ref(ctx),
                            encounter=_encounter_ref(ctx, v),
                            effectiveDateTime=_d(v.visit_date),
                            valueQuantity=_qty(getattr(vt, attr), unit),
                        ),
                        vt,
                    )
                )
        return out


class LabObservationEmitter:
    """LOINC-coded lab Observations with ranges and interpretation."""

    resource_type: ClassVar[str] = "Observation"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build lab Observations (typed)."""
        out = []
        for v in ctx.patient.visits:
            for seq, lr in enumerate(v.lab_results):
                out.append(
                    (
                        Observation(
                            id=ctx.rid("Observation", v.id, "lab", lr.loinc_code, seq),
                            status="final",
                            category=[
                                CodeableConcept(
                                    coding=[Coding(system=SYSTEMS["obs-category"], code="laboratory")]
                                )
                            ],
                            code=_loinc(lr.loinc_code, lr.test_name),
                            subject=_patient_ref(ctx),
                            encounter=_encounter_ref(ctx, v),
                            effectiveDateTime=_d(v.visit_date),
                            valueQuantity=_qty(lr.value, lr.unit),
                            referenceRange=[
                                ObservationReferenceRange(
                                    low=_qty(lr.reference_low, lr.unit),
                                    high=_qty(lr.reference_high, lr.unit),
                                )
                            ],
                            interpretation=[
                                CodeableConcept(
                                    coding=[
                                        Coding(
                                            system=SYSTEMS["interpretation"],
                                            code=str(lr.status).split(".")[-1].upper()[0],
                                        )
                                    ]
                                )
                            ],
                        ),
                        lr,
                    )
                )
        return out


class MedicationRequestEmitter:
    """MedicationRequest per prescription (the order events)."""

    resource_type: ClassVar[str] = "MedicationRequest"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build MedicationRequest resources (typed)."""
        out = []
        for v in ctx.patient.visits:
            for seq, rx in enumerate(v.prescriptions):
                out.append(
                    (
                        MedicationRequest(
                            id=ctx.rid("MedicationRequest", v.id, rx.drug_name, seq),
                            status="active",
                            intent="order",
                            medicationCodeableConcept=CodeableConcept(text=f"{rx.drug_name} {rx.dose}"),
                            subject=_patient_ref(ctx),
                            encounter=_encounter_ref(ctx, v),
                            authoredOn=_d(v.visit_date),
                            dosageInstruction=[
                                Dosage(
                                    text=f"{rx.dose} {rx.frequency}",
                                    timing=Timing(repeat=TimingRepeat(frequency=1)),
                                )
                            ],
                            dispenseRequest=MedicationRequestDispenseRequest(
                                numberOfRepeatsAllowed=rx.refills
                            ),
                            note=[Annotation(text=rx.drug_class)],
                        ),
                        rx,
                    )
                )
        return out


class AllergyIntoleranceEmitter:
    """Structured allergies (v0.4.0)."""

    resource_type: ClassVar[str] = "AllergyIntolerance"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build AllergyIntolerance resources (typed)."""
        out = []
        for a in ctx.patient.allergies:
            severity = str(a.severity).split(".")[-1].lower() if a.severity else None
            resource = AllergyIntolerance(
                id=ctx.rid("AllergyIntolerance", a.substance),
                clinicalStatus=CodeableConcept(
                    coding=[Coding(system=SYSTEMS["allergy-clinical"], code="active")]
                ),
                code=CodeableConcept(text=a.substance),
                patient=_patient_ref(ctx),
                recordedDate=_d(a.noted_date),
            )
            if a.reaction:
                resource.reaction = [
                    AllergyIntoleranceReaction(
                        manifestation=[CodeableConcept(text=a.reaction)],
                        severity=severity,
                    )
                ]
            out.append((resource, a))
        return out


class FamilyMemberHistoryEmitter:
    """Structured family history + lightweight relatives' narrative notes."""

    resource_type: ClassVar[str] = "FamilyMemberHistory"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build FamilyMemberHistory resources (typed)."""
        out = []
        for h in ctx.patient.family_history:
            condition = FamilyMemberHistoryCondition(
                code=CodeableConcept(
                    coding=(
                        [Coding(system=SYSTEMS["icd10"], code=h.icd10_code, display=h.condition)]
                        if h.icd10_code
                        else []
                    ),
                    text=h.condition,
                )
            )
            if h.onset_age is not None:
                condition.onsetAge = Age(value=Decimal(str(h.onset_age)), unit="a")
            resource = FamilyMemberHistory(
                id=ctx.rid("FamilyMemberHistory", h.relationship_type, h.condition),
                status="completed",
                patient=_patient_ref(ctx),
                relationship=CodeableConcept(text=h.relationship_type),
                condition=[condition],
            )
            member = h.family_member
            if member is not None and member.summary:
                resource.note = [Annotation(text=member.summary)]
            out.append((resource, h))
        return out


class MedicationStatementEmitter:
    """The cross-visit medication list (v0.4.0)."""

    resource_type: ClassVar[str] = "MedicationStatement"

    STATUS = {"ACTIVE": "active", "COMPLETED": "completed", "STOPPED": "stopped"}

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build MedicationStatement resources (typed)."""
        out = []
        for m in ctx.patient.medications:
            resource = MedicationStatement(
                id=ctx.rid("MedicationStatement", m.drug_name, m.start_date),
                status=self.STATUS.get(str(m.status).split(".")[-1], "active"),
                medicationCodeableConcept=CodeableConcept(text=f"{m.drug_name} {m.dose or ''}".strip()),
                subject=_patient_ref(ctx),
                effectivePeriod=Period(start=_d(m.start_date), end=_d(m.end_date)),
                dosage=[Dosage(text=f"{m.dose or ''} {m.frequency or ''}".strip())],
            )
            if m.indication is not None:
                resource.reasonCode = [CodeableConcept(text=m.indication.description)]
            out.append((resource, m))
        return out


class ProcedureEmitter:
    """Performed procedures (v0.4.0) — the SNOMED intervention slot."""

    resource_type: ClassVar[str] = "Procedure"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build Procedure resources (typed)."""
        out = []
        for proc in ctx.patient.procedures:
            resource = FhirProcedure(
                id=ctx.rid("Procedure", proc.description, proc.performed_date),
                status="completed",
                code=CodeableConcept(text=proc.description),
                subject=_patient_ref(ctx),
                performedDateTime=_d(proc.performed_date),
            )
            if proc.visit is not None:
                resource.encounter = _encounter_ref(ctx, proc.visit)
            out.append((resource, proc))
        return out


class ImmunizationEmitter:
    """CVX-coded immunization history (v0.4.0)."""

    resource_type: ClassVar[str] = "Immunization"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build Immunization resources (typed)."""
        out = []
        for imm in ctx.patient.immunizations:
            resource = Immunization(
                id=ctx.rid("Immunization", imm.vaccine, imm.administered_date, imm.dose_number),
                status="completed",
                vaccineCode=CodeableConcept(
                    coding=(
                        [Coding(system=SYSTEMS["cvx"], code=imm.cvx_code, display=imm.vaccine)]
                        if imm.cvx_code
                        else []
                    ),
                    text=imm.vaccine,
                ),
                patient=_patient_ref(ctx),
                occurrenceDateTime=_d(imm.administered_date),
            )
            if imm.dose_number is not None:
                resource.protocolApplied = [
                    ImmunizationProtocolApplied(doseNumberPositiveInt=imm.dose_number)
                ]
            out.append((resource, imm))
        return out


class DocumentReferenceEmitter:
    """Stored visit notes as DocumentReference (review decision Q2)."""

    resource_type: ClassVar[str] = "DocumentReference"

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build DocumentReference resources (typed) from stored notes."""
        out = []
        for v in ctx.patient.visits:
            for note in v.notes:
                out.append(
                    (
                        DocumentReference(
                            id=ctx.rid("DocumentReference", v.id, note.note_type),
                            status="current",
                            type=CodeableConcept(text=f"{str(note.note_type).split('.')[-1]} note"),
                            subject=_patient_ref(ctx),
                            date=_instant(v.visit_date),
                            context=DocumentReferenceContext(encounter=[_encounter_ref(ctx, v)]),
                            content=[
                                DocumentReferenceContent(
                                    attachment=Attachment(
                                        contentType="text/plain",
                                        data=base64.b64encode(note.text.encode()),
                                    )
                                )
                            ],
                        ),
                        note,
                    )
                )
        return out
