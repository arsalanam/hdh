"""LOINC's OntologyService — implementation #3 of the protocol.

Hierarchy strategy here is the materialized ``path``, because that is the
shape LOINC ships: ``MultiAxialHierarchy.csv`` gives a dotted
``PATH_TO_ROOT`` rather than a parent table, so the tree costs a column
and not a closure build. Like ICD-10-CM's, the path is THIS module's
private business — nothing outside reads it (design snomed-module.md §6).

The funnel repeats what issue #54 measured on SNOMED, because the lessons
were about lexical retrieval rather than about SNOMED:

- exact term first, as its own query, so a common name cannot be pushed
  out of the pool by a hundred FTS ties;
- a fuzzy match must account for EVERY word of the mention, or it is a
  confident answer to half the question and gets capped below the review
  threshold;
- and LOINC's own ``RELATEDNAMES2`` is the abbreviation table we would
  otherwise curate — "A1c", "HgbA1c" and "Hemoglobin A1c" are all in the
  release.

The axis preference is the one thing genuinely new here. A bare "sodium"
matches serum, urine, CSF and dialysis-fluid codes equally on text, and
they are different tests; ``context["system"]`` lets a caller say which
specimen it meant, and blood wins by default because that is what an
unqualified order means in family medicine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select

from hdh.core.ontology import Candidate, Concept

ONTOLOGY = "loinc"

_TERM_TYPE_RANK = {"preferred": 0, "synonym": 1}

#: Specimens an unqualified order means in a family practice, best first.
#: A "sodium" with no specimen is a serum sodium; nobody orders a CSF
#: sodium without saying so.
_DEFAULT_SYSTEMS = ("ser/plas", "bld", "ser", "plas", "bld/plas")


def build_service(session: Any) -> LoincOntologyService:
    """Discovery hook for hdh.core.ontology.get_ontology_service()."""
    return LoincOntologyService(session)


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_terms"]


@dataclass
class LoincOntologyService:
    """The protocol over a LOINC release (see module docstring)."""

    session: Any
    ontology: ClassVar[str] = ONTOLOGY

    # ── protocol ─────────────────────────────────────────────────────────

    def lookup(self, code: str) -> Concept | None:
        row = self._row(code)
        return self._concept(row) if row is not None else None

    def ancestors(self, code: str) -> tuple[Concept, ...]:
        """Every parent up the multiaxial path, nearest first.

        Empty when the release was loaded without its hierarchy file —
        which is honest: LOINC is usable for coding without the tree, and
        pretending otherwise would invent relationships.
        """
        row = self._row(code)
        if row is None or not row.path:
            return ()
        concepts_t, _terms = _tables()
        segments = [s for s in row.path.split(".") if s and s != row.code]
        if not segments:
            return ()
        found = self.session.execute(
            select(concepts_t).where(concepts_t.c.ontology == ONTOLOGY, concepts_t.c.code.in_(segments))
        ).all()
        by_code = {r.code: self._concept(r) for r in found}
        # nearest first: the path runs root → leaf, so walk it backwards
        return tuple(by_code[c] for c in reversed(segments) if c in by_code)

    def descendants(self, code: str) -> tuple[Concept, ...]:
        """Everything under this node — one prefix scan on the path."""
        row = self._row(code)
        if row is None:
            return ()
        concepts_t, _terms = _tables()
        prefix = f"{row.path}." if row.path else f"{row.code}."
        rows = self.session.execute(
            select(concepts_t)
            .where(
                concepts_t.c.ontology == ONTOLOGY,
                concepts_t.c.path.like(f"{prefix}%"),
                concepts_t.c.code != row.code,
            )
            .order_by(concepts_t.c.hierarchy_depth, concepts_t.c.code)
        ).all()
        return tuple(self._concept(r) for r in rows)

    def subsumes(self, ancestor_code: str, descendant_code: str) -> bool:
        """True if the descendant's path runs through the ancestor."""
        parent, child = self._row(ancestor_code), self._row(descendant_code)
        if parent is None or child is None or not child.path or parent.code == child.code:
            return False
        return f".{parent.code}." in f".{child.path}."

    def synonyms(self, code: str) -> tuple[str, ...]:
        """Every term naming this code, preferred first."""
        _concepts, terms_t = _tables()
        rows = self.session.execute(
            select(terms_t.c.term, terms_t.c.term_type)
            .where(terms_t.c.concept_id == f"{ONTOLOGY}:{code}", terms_t.c.active)
            .order_by(terms_t.c.term)
        ).all()
        ranked = sorted(rows, key=lambda r: _TERM_TYPE_RANK.get(str(r.term_type), 9))
        seen: dict[str, None] = {}
        for row in ranked:
            seen.setdefault(row.term, None)
        return tuple(seen)

    def normalize(self, mention: str, context: dict | None = None) -> tuple[Candidate, ...]:
        """Ranked LOINC candidates, run by :mod:`hdh.core.termsearch`.

        The rungs and the scoring are shared with every other vocabulary.
        What stays here is what only LOINC knows: the specimen axis, which
        separates codes that text alone cannot.

        ``context`` keys (all optional): ``limit``; ``system`` — the
        specimen the caller meant ("ser/plas", "urine"); ``classes`` —
        LOINC CLASS values to prefer (e.g. "CHEM").
        """
        from hdh.core import termsearch

        return termsearch.search(self.session, self._profile(), mention, context)

    def _profile(self):
        """This vocabulary's shape, for the shared funnel.

        No abbreviation separator: LOINC keeps its abbreviations as
        ordinary synonyms in RELATEDNAMES2 rather than spelling them out
        inside the term, so there is no ``ABBR - Expansion`` rung to run.
        """
        from hdh.core.termsearch import SearchProfile

        return SearchProfile(
            ontology=ONTOLOGY,
            term_type_rank=_TERM_TYPE_RANK,
            abbreviation_separator=None,
            adjust=self._adjust,
        )

    def _adjust(self, concept: Concept, context) -> list[tuple[float, str]]:
        """The specimen and class preferences only LOINC can apply.

        A bare "sodium" matches serum, urine, CSF and dialysate equally on
        text, and they are different tests. A caller who means urine says
        so; one who says nothing means blood, because that is what an
        unqualified order means in family medicine.
        """
        out: list[tuple[float, str]] = []
        system = (concept.properties.get("system") or "").lower()
        wanted_system = (context.get("system") or "").strip().lower()
        if wanted_system:
            if wanted_system in system:
                out.append((0.15, f"system: {system}"))
            elif system:
                out.append((-0.15, ""))
        elif system in _DEFAULT_SYSTEMS:
            out.append((0.05, "default specimen"))

        wanted_classes = {c.upper() for c in context.get("classes", ())}
        if wanted_classes and (concept.properties.get("class") or "").upper() in wanted_classes:
            out.append((0.1, "class"))
        return out

    def _row(self, code: str):
        concepts_t, _terms = _tables()
        return self.session.execute(select(concepts_t).where(concepts_t.c.id == f"{ONTOLOGY}:{code}")).first()

    def _concept(self, row) -> Concept:
        mapping = row._mapping
        return Concept(
            id=mapping["id"],
            ontology=mapping["ontology"],
            code=mapping["code"],
            display=mapping["display"],
            kind=mapping["kind"],
            properties=mapping["properties"] or {},
        )
