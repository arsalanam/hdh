"""The ontology module's FHIR contribution: SNOMED codings on Conditions.

This replaces core's old ``_dx_codings`` getattr hack — the module that
OWNS the snomed columns (added via the schema registry) now enriches the
resources that need them. Core never knows.
"""

from typing import Any, ClassVar


class ConditionCodingEnricher:
    """Append the SNOMED coding to Condition resources when tagged."""

    resource_type: ClassVar[str] = "Condition"

    def enrich(self, resource: dict, entity: Any, ctx) -> None:
        """Additive only: appends a coding; never touches existing ones."""
        snomed = getattr(entity, "snomed_code", None) if entity is not None else None
        if not snomed:
            return
        resource["code"]["coding"].append(
            {
                "system": "http://snomed.info/sct",
                "code": snomed,
                "display": getattr(entity, "snomed_display", None) or entity.description,
            }
        )


def fhir_enrichers() -> list:
    """Discovery hook consumed by hdh.core.fhir.module_enrichers()."""
    return [ConditionCodingEnricher()]
