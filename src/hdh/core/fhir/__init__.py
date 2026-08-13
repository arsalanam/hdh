"""Pluggable FHIR R4 export (design: docs/design/fhir-emitters.md).

Core ships one small :class:`FhirEmitter` per resource type; feature
modules contribute :class:`FhirEnricher`\\s through ``FHIR_MODULES``
discovery — the schema-registry move applied to output. Resource ids are
**stable content hashes** (review decision Q1): re-exporting an unchanged
chart yields identical ids, so bundles diff cleanly.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import Any, ClassVar, Protocol

from hdh.core.models import Patient

log = logging.getLogger("hdh.fhir")

_ENTITY_KEY = "_entity"  # transient link resource → source entity for enrichers


def stable_id(*parts) -> str:
    """A deterministic 12-hex id from the resource's natural keys."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class ExportContext:
    """Per-export state shared by emitters and enrichers."""

    patient: Patient
    mrn: str

    def rid(self, resource_type: str, *parts) -> str:
        """Stable resource id: ``{mrn}-{type}-{hash(natural keys)}``."""
        return f"{self.mrn}-{resource_type.lower()}-{stable_id(self.mrn, resource_type, *parts)}"

    def encounter_ref(self, visit) -> dict:
        """The reference to a visit's Encounter — derivable by any emitter,
        so emitter ordering never matters."""
        return {"reference": f"Encounter/{self.rid('Encounter', visit.id)}"}


class FhirEmitter(Protocol):
    """Builds all resources of one type for a patient's chart."""

    resource_type: ClassVar[str]

    def emit(self, ctx: ExportContext) -> list[dict]:
        """Return resources; each may carry ``_entity`` for enrichers."""
        ...


class FhirEnricher(Protocol):
    """Decorates already-built resources of one type. ADDITIVE ONLY:
    enrichers may append codings/extensions/fields but must never delete
    or replace what an emitter built (test-enforced)."""

    resource_type: ClassVar[str]

    def enrich(self, resource: dict, entity: Any, ctx: ExportContext) -> None:
        """Mutate ``resource`` in place with additions."""
        ...


def _core_emitters() -> list[FhirEmitter]:
    from hdh.core.fhir import emitters as e

    return [
        e.PatientEmitter(),
        e.PractitionerEmitter(),
        e.EncounterEmitter(),
        e.ConditionEmitter(),
        e.VitalsObservationEmitter(),
        e.LabObservationEmitter(),
        e.MedicationRequestEmitter(),
        e.AllergyIntoleranceEmitter(),
        e.FamilyMemberHistoryEmitter(),
        e.MedicationStatementEmitter(),
        e.ProcedureEmitter(),
        e.ImmunizationEmitter(),
        e.DocumentReferenceEmitter(),
    ]


def module_enrichers(strict: bool = False) -> list[FhirEnricher]:
    """Enrichers contributed by feature modules (``FHIR_MODULES``).

    Runtime is fail-soft (an absent optional module never breaks export);
    tests load with ``strict=True`` so real defects fail loud (review
    decision Q3).
    """
    from hdh.modules import FHIR_MODULES

    enrichers: list[FhirEnricher] = []
    for module_path in FHIR_MODULES:
        try:
            module = import_module(module_path)
            enrichers.extend(module.fhir_enrichers())
        except Exception:
            if strict:
                raise
            log.warning("FHIR enricher module %s failed to load — skipped", module_path)
    return enrichers


def build_bundle(patient: Patient, strict: bool = False) -> dict:
    """Assemble the patient's FHIR R4 Bundle: every emitter, then every
    matching enricher (additive), then the Bundle wrapper."""
    ctx = ExportContext(patient=patient, mrn=patient.mrn)
    resources: list[dict] = []
    for emitter in _core_emitters():
        resources.extend(emitter.emit(ctx))

    by_type: dict[str, list[dict]] = {}
    for resource in resources:
        by_type.setdefault(resource["resourceType"], []).append(resource)
    for enricher in module_enrichers(strict=strict):
        for resource in by_type.get(enricher.resource_type, []):
            enricher.enrich(resource, resource.get(_ENTITY_KEY), ctx)

    for resource in resources:
        resource.pop(_ENTITY_KEY, None)

    return {
        "resourceType": "Bundle",
        "id": f"bundle-{patient.mrn}",
        "type": "collection",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }
