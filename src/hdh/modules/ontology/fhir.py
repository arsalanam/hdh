"""The ontology module's FHIR contribution: SNOMED codings on Conditions.

This replaces core's old ``_dx_codings`` getattr hack — the module that
OWNS the snomed columns (added via the schema registry) now enriches the
resources that need them. Core never knows.
"""

from functools import lru_cache
from typing import Any, ClassVar

#: SNOMED hierarchies a FHIR ``Condition.code`` may carry. Its value set is
#: problems, diagnoses and health concerns — a *situation with explicit
#: context* qualifies ("History of cerebrovascular accident" is a
#: problem-list entry), a *procedure* does not.
CHARTABLE_TAGS = frozenset({"disorder", "finding", "situation"})


@lru_cache(maxsize=1)
def _non_chartable_codes() -> frozenset[str]:
    """SNOMED codes the catalog maps that are NOT problems.

    Every ICD code the generator emits maps to a SNOMED concept, which is
    what makes ICD→SNOMED coverage complete — but six of them are encounter
    reasons and one is an event: an annual physical is
    `162673000 General examination of patient` (procedure) and a fall is
    `217082002 Accidental fall` (event). Correct mappings, and not problems.

    Read from the catalog rather than restated here, so the two cannot
    drift: the profile author records the hierarchy alongside the code, and
    this is the consumer that needs it.
    """
    from hdh.core.conditions import default_catalog

    catalog = default_catalog()
    codes = set()
    for name in catalog.names():
        profile = catalog.get(name)
        if profile.snomed_code and profile.snomed_tag not in CHARTABLE_TAGS:
            codes.add(profile.snomed_code)
    return frozenset(codes)


class ConditionCodingEnricher:
    """Append the SNOMED coding to Condition resources when tagged."""

    resource_type: ClassVar[str] = "Condition"

    def enrich(self, resource: Any, entity: Any, ctx) -> None:
        """Additive only: appends a typed Coding; never touches existing ones.

        A concept outside :data:`CHARTABLE_TAGS` is skipped rather than
        appended. The ICD coding still describes the encounter, and the
        SNOMED mapping still exists for anyone who asks the ontology for it
        — it simply does not claim to be the patient's problem.
        """
        from fhir.resources.R4B.coding import Coding

        snomed = getattr(entity, "snomed_code", None) if entity is not None else None
        if not snomed or snomed in _non_chartable_codes():
            return
        resource.code.coding.append(
            Coding(
                system="http://snomed.info/sct",
                code=snomed,
                display=getattr(entity, "snomed_display", None) or entity.description,
            )
        )


def fhir_enrichers() -> list:
    """Discovery hook consumed by hdh.core.fhir.module_enrichers()."""
    return [ConditionCodingEnricher()]
