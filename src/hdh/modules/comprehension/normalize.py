"""Stage 3: each mention → its home ontology's normalize() funnel.

Routing (master design §4): PROBLEM / PROCEDURE / ALLERGY go to the
SNOMED service through the core protocol, semantic-tag constrained per
type. LAB_VITAL and MEDICATION use **documented placeholders** until the
LOINC and RxNorm modules land (master §11–§12): a deterministic LOINC
alias map derived from our own terminology plus the condition catalog's
LabSpecs, and the catalog's drug names. Placeholder codes carry no
``concept_id`` FK (those catalogs are not in the shared tables) — the
code travels in the mention's properties instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from hdh.modules.comprehension.contracts import Mention, MentionType

# semantic-tag constraint per mention type for the SNOMED funnel
_SNOMED_TAGS: dict[MentionType, tuple[str, ...]] = {
    MentionType.PROBLEM: ("disorder", "finding"),
    MentionType.PROCEDURE: ("procedure",),
    MentionType.ALLERGY: ("substance", "product"),
}

# The vitals panel, named as clinicians write it → LOINC (our own
# terminology constants; deterministic, no catalog required).
VITAL_ALIASES: dict[str, tuple[str, str]] = {
    "bp": ("55284-4", "Blood pressure"),
    "blood pressure": ("55284-4", "Blood pressure"),
    "hr": ("8867-4", "Heart rate"),
    "heart rate": ("8867-4", "Heart rate"),
    "pulse": ("8867-4", "Heart rate"),
    "rr": ("9279-1", "Respiratory rate"),
    "respirations": ("9279-1", "Respiratory rate"),
    "respiratory rate": ("9279-1", "Respiratory rate"),
    "t": ("8310-5", "Body temperature"),
    "temp": ("8310-5", "Body temperature"),
    "temperature": ("8310-5", "Body temperature"),
    "spo2": ("59408-5", "Oxygen saturation"),
    "oxygen saturation": ("59408-5", "Oxygen saturation"),
    "o2 sat": ("59408-5", "Oxygen saturation"),
    "wt": ("29463-7", "Body weight"),
    "weight": ("29463-7", "Body weight"),
    "ht": ("8302-2", "Body height"),
    "height": ("8302-2", "Body height"),
    "bmi": ("39156-5", "Body mass index"),
    "pain": ("72514-3", "Pain severity score"),
}


@dataclass(frozen=True)
class Code:
    """One assigned code with its provenance."""

    system: str  # "snomed_ct" | "loinc" | "drug-catalog"
    code: str
    display: str
    score: float
    in_shared_tables: bool  # True → concept_id FK is valid


def _lab_aliases() -> dict[str, tuple[str, str]]:
    """LOINC aliases for every lab the condition catalog can order —
    derived from the packs' LabSpecs, so new packs extend it for free."""
    from hdh.core.conditions import default_catalog

    aliases = dict(VITAL_ALIASES)
    catalog = default_catalog()
    for name in catalog.names():
        for spec in catalog.get(name).labs:
            aliases.setdefault(spec.test_name.lower(), (spec.loinc_code, spec.test_name))
    return aliases


def _drug_names() -> dict[str, str]:
    """Every drug name the catalog can prescribe (RxNorm's placeholder)."""
    from hdh.core.conditions import default_catalog

    names: dict[str, str] = {}
    catalog = default_catalog()
    for name in catalog.names():
        for rx in catalog.get(name).rx_options:
            head = rx.drug_name.split(" (")[0]
            names.setdefault(head.lower(), rx.drug_name)
    return names


class MentionNormalizer:
    """Stage-3 dispatcher: one instance per comprehension run (caches the
    alias tables and the SNOMED service handle)."""

    def __init__(self, session) -> None:
        from hdh.core.ontology import get_ontology_service

        self._session = session
        self._snomed = get_ontology_service("snomed_ct", session)
        self._labs = _lab_aliases()
        self._drugs = _drug_names()

    def candidates(self, mention: Mention) -> tuple[Code, ...]:
        """Ranked codes for one mention (empty = honestly unlinked)."""
        if mention.mention_type in _SNOMED_TAGS:
            found = self._snomed.normalize(
                mention.text,
                {"semantic_tags": list(_SNOMED_TAGS[mention.mention_type]), "limit": 3},
            )
            return tuple(
                Code(
                    system="snomed_ct",
                    code=c.concept.code,
                    display=c.concept.display,
                    score=c.score,
                    in_shared_tables=True,
                )
                for c in found
            )
        if mention.mention_type is MentionType.LAB_VITAL:
            hit = self._labs.get(mention.text.strip().lower())
            if hit:
                code, display = hit
                return (Code("loinc", code, display, 1.0, in_shared_tables=False),)
            return ()
        if mention.mention_type is MentionType.MEDICATION:
            drug = self._drugs.get(mention.text.strip().lower())
            if drug:
                return (Code("drug-catalog", drug, drug, 1.0, in_shared_tables=False),)
            return ()
        return ()
