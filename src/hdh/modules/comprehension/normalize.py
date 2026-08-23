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

import re
from dataclasses import dataclass

from hdh.modules.comprehension.contracts import AttributeKind, Mention, MentionType

# semantic-tag constraint per mention type for the SNOMED funnel
_SNOMED_TAGS: dict[MentionType, tuple[str, ...]] = {
    MentionType.PROBLEM: ("disorder", "finding"),
    MentionType.PROCEDURE: ("procedure",),
    MentionType.ALLERGY: ("substance", "product"),
}

# The vitals panel, named as clinicians write it → LOINC.
#
# This is NOT the general lab vocabulary any more — that is the LOINC
# module's job now (design service-requests §7). What survives here is
# narrow and load-bearing: these codes are the contract with the chart
# itself. `applier._VITAL_COLUMNS` keys off them to decide which column of
# the vitals row a reading belongs in, so "HR" must resolve to 8867-4 and
# not to whichever heart-rate code a term search likes best.
#
# Everything the table does not cover — "B/P", "Tmax", any real lab — now
# falls through to the LOINC funnel, which is what §12 recorded as the
# brittleness worth fixing.
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


#: Displays that assert an outcome. A note saying the exam was NORMAL must
#: not be coded to one of these — 'Diabetic foot examination not done' and
#: 'Sight deteriorating' are not near-misses, they are the opposite of what
#: was written, and both scored above the review line (#72).
_NEGATIVE_OUTCOMES = (
    "not done",
    "not performed",
    "declined",
    "refused",
    "abnormal",
    "deteriorat",
    "worsening",
)

#: What a note says when an exam was unremarkable. Matched on WORD
#: boundaries: "abnormal" contains "normal", and a substring test read an
#: abnormal exam as a normal one — then refused the abnormal concept for
#: contradicting a claim the note never made.
_BOUNDARY = chr(92) + "b"  # a literal word boundary (see trap: escaping)
_NORMAL_RE = re.compile(_BOUNDARY + r"(?:normal|unremarkable|nad|no abnormalit(?:y|ies))" + _BOUNDARY)


def _contradicts_the_note(mention: Mention, display: str) -> bool:
    """Does this candidate assert the opposite of what the note said?

    Only ever REMOVES a candidate, and only when the note made a positive
    statement to contradict. Silence is not an assertion: a mention with no
    interpretation attribute constrains nothing, and this returns False.

    The funnel cannot make this call — lexically 'Diabetic foot examination
    not done' is an excellent match for "foot exam", which is exactly the
    problem. It takes the note's own words to know the answer is wrong.
    """
    interpretation = _attribute(mention, AttributeKind.INTERPRETATION)
    if not interpretation:
        return False
    if not _NORMAL_RE.search(interpretation.lower()):
        return False
    lowered = display.lower()
    return any(phrase in lowered for phrase in _NEGATIVE_OUTCOMES)


def _attribute(mention: Mention, kind) -> str | None:
    """One attribute's text, or None."""
    return next((a.text for a in mention.attributes if a.kind is kind), None)


def _full_phrase(mention: Mention) -> str:
    """The mention plus its own attributes, as written.

    ``resolve`` uses this for the release form: "ER" is part of the drug's
    name in a note and part of the dose form in RxNorm, so the phrase has to
    survive intact even though the pieces are separate spans.
    """
    return " ".join([mention.text, *(a.text for a in mention.attributes)])


class MentionNormalizer:
    """Stage-3 dispatcher: one instance per comprehension run (caches the
    alias tables and the SNOMED service handle)."""

    def __init__(self, session) -> None:
        from hdh.core.ontology import get_ontology_service

        self._session = session
        self._snomed = get_ontology_service("snomed_ct", session)
        self._labs = _lab_aliases()
        self._drugs = _drug_names()
        self._loinc = self._optional_service(session, "loinc")
        self._rxnorm = self._optional_service(session, "rxnorm")

    @staticmethod
    def _optional_service(session, ontology: str):
        """A licensed vocabulary's service, or None when no release is loaded.

        LOINC and RxNorm are both licensed, so most installations will not
        have them and comprehension has to keep working without either — the
        alias tables above are what it falls back to.
        """
        from sqlalchemy import func, select

        from hdh.core.models import Base
        from hdh.core.ontology import get_ontology_service

        concepts = Base.metadata.tables["ontology_concepts"]
        loaded = session.execute(
            select(func.count()).select_from(concepts).where(concepts.c.ontology == ontology)
        ).scalar()
        return get_ontology_service(ontology, session) if loaded else None

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
                if not _contradicts_the_note(mention, c.concept.display)
            )
        if mention.mention_type is MentionType.LAB_VITAL:
            hit = self._labs.get(mention.text.strip().lower())
            if hit:
                # The vitals contract wins: these codes decide which column
                # of the vitals row the reading lands in.
                code, display = hit
                return (Code("loinc", code, display, 1.0, in_shared_tables=False),)
            if self._loinc is not None:
                # Everything else — "B/P", "Tmax", a real lab — resolves by
                # term search instead of failing to resolve at all.
                found = self._loinc.normalize(mention.text, {"limit": 3})
                return tuple(
                    Code(
                        system="loinc",
                        code=c.concept.code,
                        display=c.concept.display,
                        score=c.score,
                        in_shared_tables=True,
                    )
                    for c in found
                )
            return ()
        if mention.mention_type is MentionType.MEDICATION:
            return self._medication_codes(mention)
        return ()

    def _medication_codes(self, mention: Mention) -> tuple[Code, ...]:
        """RxNorm if a catalog is loaded, the generator's name table if not.

        The name table is not a terminology: ``drug-catalog:Lisinopril`` has
        no RXCUI, no ingredient and no relationship to anything, and a drug
        it does not list resolves to nothing at all. It stays as the offline
        fallback and nothing more.

        This goes through ``coding.resolve`` rather than the funnel directly
        because resolve is where the module's judgement lives: stop at the
        deepest level the EVIDENCE supports, walk to a branded product when
        the name is a brand, and refuse rather than invent a strength or a
        form the note did not give. A drug is exactly where a confident
        guess does the most harm.
        """
        if self._rxnorm is not None:
            from hdh.modules.rxnorm.coding import resolve

            coding = resolve(
                self._rxnorm,
                mention.text,
                # The note already separated these out; handing them over is
                # what lets the level ladder work instead of guessing from
                # string length (design §5).
                strength=_attribute(mention, AttributeKind.DOSE),
                route=_attribute(mention, AttributeKind.ROUTE),
                raw=_full_phrase(mention),
            )
            if coding is not None:
                return (
                    Code(
                        system="rxnorm",
                        code=coding.rxcui,
                        display=coding.display,
                        score=1.0,
                        in_shared_tables=True,
                    ),
                )
        drug = self._drugs.get(mention.text.strip().lower())
        if drug:
            return (Code("drug-catalog", drug, drug, 1.0, in_shared_tables=False),)
        return ()
