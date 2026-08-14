"""The OntologyService protocol — one typed interface per vocabulary.

Every ontology module (icd10cm, snomed, later rxnorm/loinc) implements
this protocol and registers in ``hdh.modules.ONTOLOGY_MODULES``; consumers
(comprehension, the pattern compiler, agent tools) dispatch through
:func:`get_ontology_service` and never touch storage strategy (design
notes-comprehension-service.md §4–§5, snomed-module.md §7–§8).

Per-ontology hierarchy strategy is private: ICD-10-CM keeps its
materialized ``path`` tree; SNOMED CT stores ``parent_of`` edges plus a
transitive-closure table. Tree-only columns are NULL for DAG rows *by
contract* — and helpers raise :class:`UnsupportedHierarchy` rather than
returning empty when asked a tree question of a DAG ontology, so misuse
fails loudly, never silently empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Protocol, runtime_checkable


class UnsupportedHierarchy(Exception):
    """A hierarchy-strategy helper was invoked for an ontology whose data
    does not support it (e.g. tree ``path`` math on a DAG ontology).
    Loud-and-diagnosable replaces empty-but-wrong (design §5 item 3)."""


@dataclass(frozen=True)
class Concept:
    """One ontology concept, storage-independent."""

    id: str  # "<ontology>:<code>"
    ontology: str
    code: str
    display: str
    kind: str
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """One normalize() suggestion: a concept, its score, and why."""

    concept: Concept
    score: float
    reason: str = ""


@runtime_checkable
class OntologyService(Protocol):
    """The vocabulary contract every ontology module implements.

    ``ancestors``/``descendants`` return SETS, not chains — a chain is the
    degenerate tree case. ``subsumes`` is on the protocol because
    disambiguation and care-gap rules both want it cheap (one closure hit
    for SNOMED, one prefix test for ICD).
    """

    ontology: str

    def lookup(self, code: str) -> Concept | None:
        """The concept for a code, or None if absent from the catalog."""
        ...

    def ancestors(self, code: str) -> tuple[Concept, ...]:
        """Every ancestor of the code (transitive, excluding itself)."""
        ...

    def descendants(self, code: str) -> tuple[Concept, ...]:
        """Every descendant of the code (transitive, excluding itself)."""
        ...

    def synonyms(self, code: str) -> tuple[str, ...]:
        """Every term naming this concept, preferred term first."""
        ...

    def normalize(self, mention: str, context: dict | None = None) -> tuple[Candidate, ...]:
        """Ranked concept candidates for a free-text mention (the funnel)."""
        ...

    def subsumes(self, ancestor_code: str, descendant_code: str) -> bool:
        """True if ancestor_code is a (transitive) ancestor of descendant_code."""
        ...


def get_ontology_service(ontology: str, session: Any) -> OntologyService:
    """Dispatch to the owning module's service — the ONLY sanctioned path
    to hierarchy answers outside a module's own implementation.

    Raises LookupError (loudly, with the registered names) for an unknown
    ontology rather than letting a consumer fall through to silently-empty
    direct SQL."""
    from hdh.modules import ONTOLOGY_MODULES

    module_path = ONTOLOGY_MODULES.get(ontology)
    if module_path is None:
        raise LookupError(
            f"no OntologyService registered for ontology '{ontology}' "
            f"(registered: {sorted(ONTOLOGY_MODULES)})"
        )
    return import_module(module_path).build_service(session)
