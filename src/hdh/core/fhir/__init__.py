"""Pluggable FHIR R4B export with typed construction (design:
docs/design/fhir-emitters.md; typed adoption decided 2026-08-13).

Core ships one small :class:`FhirEmitter` per resource type; feature
modules contribute :class:`FhirEnricher`\\s through ``FHIR_MODULES``
discovery. Emitters construct **official ``fhir.resources`` R4B models**
— malformed resources fail at the line that builds them, and every legal
field autocompletes, for human and AI authors alike. Resource ids are
stable content hashes: re-exporting an unchanged chart yields identical
ids, so bundles diff cleanly.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, ClassVar, Protocol

from hdh.core.models import Patient

log = logging.getLogger("hdh.fhir")


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

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Return ``(typed R4B resource model, source entity)`` pairs; the
        entity rides along so enrichers never re-query (None if not useful)."""
        ...


class FhirEnricher(Protocol):
    """Decorates already-built typed resources of one type. ADDITIVE ONLY:
    enrichers may append codings/extensions/fields but must never delete
    or replace what an emitter built (test-enforced)."""

    resource_type: ClassVar[str]

    def enrich(self, resource: Any, entity: Any, ctx: ExportContext) -> None:
        """Mutate the typed ``resource`` model in place with additions."""
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
    tests load with ``strict=True`` so real defects fail loud."""
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
    """Assemble the patient's FHIR R4B Bundle: every emitter (typed
    construction), then every matching enricher (additive, typed), then
    one serialization at the end."""
    from fhir.resources.R4B.bundle import Bundle, BundleEntry

    ctx = ExportContext(patient=patient, mrn=patient.mrn)
    built: list[tuple[Any, Any]] = []  # (typed model, source entity)
    for emitter in _core_emitters():
        built.extend(emitter.emit(ctx))

    by_type: dict[str, list[tuple[Any, Any]]] = {}
    for model, entity in built:
        by_type.setdefault(type(model).__name__, []).append((model, entity))
    for enricher in module_enrichers(strict=strict):
        for model, entity in by_type.get(enricher.resource_type, []):
            enricher.enrich(model, entity, ctx)

    bundle = Bundle(
        id=f"bundle-{patient.mrn}",
        type="collection",
        timestamp=datetime.now(UTC),
        total=len(built),
        entry=[BundleEntry(resource=model) for model, _entity in built],
    )
    return bundle.model_dump(mode="json", exclude_none=True)
