"""Stage 6, half two: the FHIR interchange artifact (design §10.2–§10.3).

A Composition-led **document Bundle** built with typed ``fhir.resources``
R4B construction end to end (the emitters' discipline — nested datatypes
included): every resource derives from a mention, carries a provenance
extension pointing back to its span, and validates at the line that
builds it. Per the review decision (§13 Q5) this bundle is EXPORT ONLY —
the chart applier updates hdh directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fhir.resources.R4B.allergyintolerance import AllergyIntolerance, AllergyIntoleranceReaction
from fhir.resources.R4B.bundle import Bundle, BundleEntry
from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.composition import Composition, CompositionSection
from fhir.resources.R4B.condition import Condition
from fhir.resources.R4B.dosage import Dosage
from fhir.resources.R4B.extension import Extension
from fhir.resources.R4B.medicationrequest import MedicationRequest
from fhir.resources.R4B.observation import Observation, ObservationComponent
from fhir.resources.R4B.quantity import Quantity
from fhir.resources.R4B.reference import Reference

from hdh.modules.comprehension.contracts import Assertion, AttributeKind, MentionType, RelationKind
from hdh.modules.comprehension.pipeline import ComprehendedMention, ComprehendedNote

MENTION_EXTENSION_URL = "urn:hdh:mention-span"
_BP_RE = re.compile(r"\s*(\d{2,3})\s*/\s*(\d{2,3})\s*$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _attr(item: ComprehendedMention, kind: AttributeKind) -> str | None:
    for attribute in item.mention.attributes:
        if attribute.kind is kind:
            return attribute.text
    return None


def _provenance(item: ComprehendedMention) -> list[Extension]:
    span = item.mention.span
    return [Extension(url=MENTION_EXTENSION_URL, valueString=f"{item.mention.id}:{span.start}-{span.end}")]


def _full_url(item: ComprehendedMention) -> str:
    return f"urn:hdh:mention:{item.mention.id}"


def _codeable(system: str, code: str, display: str | None = None, text: str | None = None) -> CodeableConcept:
    return CodeableConcept(coding=[Coding(system=system, code=code, display=display)], text=text)


def assemble_bundle(session, note: ComprehendedNote, subject_display: str = "Patient") -> dict:
    """The document Bundle: Composition first, one resource per applicable
    mention, TREATS relations as MedicationRequest.reasonReference."""
    subject = Reference(display=subject_display)
    resources: dict[int, Any] = {}  # mention id -> typed resource
    for item in note.mentions:
        resource = _resource_for(session, item, subject)
        if resource is not None:
            resources[item.mention.id] = resource
    _wire_treats(note, resources)

    sections: dict[str, list[Reference]] = {}
    for item in note.mentions:
        if item.mention.id not in resources:
            continue
        kind = note.extraction.section_of(item.mention).kind.value
        sections.setdefault(kind, []).append(Reference(reference=_full_url(item)))

    composition = Composition(
        status="final",
        type=_codeable("http://loinc.org", "11506-3", "Progress note"),
        date=datetime.now(UTC),
        title="Comprehended encounter note",
        author=[Reference(display="hdh comprehension pipeline")],
        subject=subject,
        section=[CompositionSection(title=kind, entry=refs) for kind, refs in sections.items()],
    )
    entries: list[BundleEntry] = [BundleEntry(fullUrl="urn:hdh:composition", resource=composition)]
    entries += [
        BundleEntry(fullUrl=_full_url(item), resource=resources[item.mention.id])
        for item in note.mentions
        if item.mention.id in resources
    ]
    bundle = Bundle(type="document", entry=entries)
    return bundle.model_dump(mode="json", exclude_none=True)


def _resource_for(session, item: ComprehendedMention, subject: Reference):
    mention_type = item.mention.mention_type
    if mention_type is MentionType.PROBLEM:
        return _condition(session, item, subject)
    if mention_type is MentionType.ALLERGY:
        return _allergy(item, subject)
    if mention_type is MentionType.LAB_VITAL:
        return _observation(item, subject)
    if mention_type is MentionType.MEDICATION:
        return _medication_request(item, subject)
    return None  # procedures join with the applier's order modeling (care-plan arc)


def _condition(session, item: ComprehendedMention, subject: Reference) -> Condition | None:
    if item.code is None or item.assertion.assertion in (
        Assertion.NEGATED,
        Assertion.FAMILY_HISTORY,
        Assertion.HYPOTHETICAL,
    ):
        return None  # absent/negated problems are not chart conditions
    from hdh.modules.comprehension.applier import _icd_for_snomed

    codings = [Coding(system="http://snomed.info/sct", code=item.code.code, display=item.code.display)]
    icd10 = _icd_for_snomed(session, item.code.code)
    if icd10:
        codings.append(Coding(system="http://hl7.org/fhir/sid/icd-10", code=icd10))
    return Condition(
        code=CodeableConcept(coding=codings, text=item.mention.text),
        subject=subject,
        extension=_provenance(item),
    )


def _allergy(item: ComprehendedMention, subject: Reference) -> AllergyIntolerance:
    resource = AllergyIntolerance(
        code=CodeableConcept(text=item.mention.text), patient=subject, extension=_provenance(item)
    )
    reaction = _attr(item, AttributeKind.REACTION)
    if reaction:
        resource.reaction = [AllergyIntoleranceReaction(manifestation=[CodeableConcept(text=reaction)])]
    return resource


def _observation(item: ComprehendedMention, subject: Reference) -> Observation | None:
    if item.code is None:
        return None
    raw = _attr(item, AttributeKind.VALUE) or ""
    unit = _attr(item, AttributeKind.UNIT)
    observation = Observation(
        status="final",
        code=_codeable("http://loinc.org", item.code.code, item.code.display),
        subject=subject,
        extension=_provenance(item),
    )
    bp = _BP_RE.match(raw)
    if item.code.code == "55284-4" and bp:
        observation.component = [
            ObservationComponent(
                code=_codeable("http://loinc.org", loinc, display),
                valueQuantity=Quantity(value=Decimal(value), unit="mm[Hg]"),
            )
            for loinc, display, value in (
                ("8480-6", "Systolic blood pressure", bp.group(1)),
                ("8462-4", "Diastolic blood pressure", bp.group(2)),
            )
        ]
        return observation
    number = _NUM_RE.search(raw)
    if number:
        observation.valueQuantity = Quantity(value=Decimal(number.group(0)), unit=unit)
    return observation


def _medication_request(item: ComprehendedMention, subject: Reference) -> MedicationRequest:
    drug = item.code.display if item.code else item.mention.text
    dose = _attr(item, AttributeKind.DOSE)
    frequency = _attr(item, AttributeKind.FREQUENCY)
    dosage_text = " ".join(part for part in (dose, frequency) if part)
    resource = MedicationRequest(
        status="active",
        intent="order",
        medicationCodeableConcept=CodeableConcept(text=f"{drug} {dose}".strip() if dose else drug),
        subject=subject,
        extension=_provenance(item),
    )
    if dosage_text:
        resource.dosageInstruction = [Dosage(text=dosage_text)]
    return resource


def _wire_treats(note: ComprehendedNote, resources: dict[int, Any]) -> None:
    """TREATS relations become MedicationRequest.reasonReference."""
    for relation in note.extraction.relations:
        if relation.kind is not RelationKind.TREATS:
            continue
        source = resources.get(relation.source_id)
        target_item = next((m for m in note.mentions if m.mention.id == relation.target_id), None)
        if source is None or target_item is None or relation.target_id not in resources:
            continue
        if hasattr(source, "reasonReference"):
            existing = list(source.reasonReference or [])
            existing.append(Reference(reference=_full_url(target_item)))
            source.reasonReference = existing
