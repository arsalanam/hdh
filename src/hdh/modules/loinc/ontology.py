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

from sqlalchemy import or_, select

from hdh.core.ontology import Candidate, Concept

ONTOLOGY = "loinc"

_TERM_TYPE_RANK = {"preferred": 0, "synonym": 1}

#: Specimens an unqualified order means in a family practice, best first.
#: A "sodium" with no specimen is a serum sodium; nobody orders a CSF
#: sodium without saying so.
_DEFAULT_SYSTEMS = ("ser/plas", "bld", "ser", "plas", "bld/plas")

#: Same ceiling, and the same reason, as the SNOMED funnel: a match that
#: explains only part of the mention is a guess about the rest, so it must
#: reach a human rather than a chart (issue #54).
PARTIAL_COVERAGE_CEILING = 0.55


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
        """Ranked LOINC candidates for a free-text mention.

        ``context`` keys (all optional): ``limit``; ``system`` — the
        specimen the caller meant ("ser/plas", "urine"), which is the axis
        that separates codes text alone cannot; ``classes`` — LOINC CLASS
        values to prefer (e.g. "CHEM").
        """
        ctx = context or {}
        limit = int(ctx.get("limit", 10))
        needle = mention.strip().lower()
        if not needle:
            return ()

        wanted_system = (ctx.get("system") or "").strip().lower()
        wanted_classes = {c.upper() for c in ctx.get("classes", ())}

        best: dict[str, tuple[float, str, bool]] = {}
        for concept_id, term, term_type, base, covered in self._search_terms(needle):
            score = base - 0.01 * _TERM_TYPE_RANK.get(term_type, 1)
            if score > best.get(concept_id, (-1.0, "", True))[0]:
                best[concept_id] = (score, term, covered)

        concepts_t, _terms = _tables()
        ranked: list[tuple[float, Candidate]] = []
        for concept_id, (score, term, covered) in best.items():
            row = self.session.execute(select(concepts_t).where(concepts_t.c.id == concept_id)).first()
            if row is None:
                continue
            concept = self._concept(row)
            reasons = [f"term: {term}"]
            system = (concept.properties.get("system") or "").lower()
            if wanted_system:
                if wanted_system in system:
                    score += 0.15
                    reasons.append(f"system: {system}")
                elif system:
                    score -= 0.15
            elif system in _DEFAULT_SYSTEMS:
                # an unqualified order means blood, and saying so beats
                # letting a urine code win on alphabetical luck
                score += 0.05
                reasons.append("default specimen")
            if wanted_classes and (concept.properties.get("class") or "").upper() in wanted_classes:
                score += 0.1
                reasons.append("class")
            if not covered:
                score = min(score, PARTIAL_COVERAGE_CEILING)
                reasons.append("partial: matches some of the mention")
            ranked.append(
                (
                    score,
                    Candidate(concept=concept, score=round(min(score, 1.0), 3), reason="; ".join(reasons)),
                )
            )
        ranked.sort(key=lambda pair: (-pair[0], pair[1].concept.code))
        return tuple(candidate for _raw, candidate in ranked[:limit])

    # ── internals ────────────────────────────────────────────────────────

    def _search_terms(self, needle: str) -> list[tuple[str, str, str, float, bool]]:
        """(concept_id, term, term_type, base score, covers_whole_mention)."""
        _concepts, terms_t = _tables()
        if self.session.get_bind().dialect.name == "postgresql":
            return self._search_postgres(needle)
        rows = self.session.execute(
            select(terms_t.c.concept_id, terms_t.c.term, terms_t.c.term_type)
            .where(
                terms_t.c.active,
                terms_t.c.concept_id.like(f"{ONTOLOGY}:%"),
                or_(
                    self._lower(terms_t.c.term) == needle,
                    self._lower(terms_t.c.term).like(f"%{needle}%"),
                ),
            )
            .limit(200)
        ).all()
        out = []
        for row in rows:
            term = row.term.lower()
            base = 1.0 if term == needle else 0.85 if term.startswith(needle) else 0.5
            out.append((row.concept_id, row.term, str(row.term_type), base, True))
        return out

    @staticmethod
    def _lower(column):
        from sqlalchemy import func

        return func.lower(column)

    def _search_postgres(self, needle: str) -> list[tuple[str, str, str, float, bool]]:
        from sqlalchemy import text as sql_text

        from hdh.modules.snomed.ontology import _covers_every_word

        exact = self.session.execute(
            sql_text(
                "SELECT concept_id, term, term_type FROM ontology_terms "
                "WHERE active AND concept_id LIKE :ns AND lower(term) = :q LIMIT 20"
            ),
            {"q": needle, "ns": f"{ONTOLOGY}:%"},
        ).all()
        out = [(r.concept_id, r.term, str(r.term_type), 1.0, True) for r in exact]
        fts = self.session.execute(
            sql_text(
                "SELECT concept_id, term, term_type, "
                "       ts_rank(to_tsvector('english', term), plainto_tsquery('english', :q), 1) AS r "
                "FROM ontology_terms WHERE active AND concept_id LIKE :ns "
                "AND to_tsvector('english', term) @@ plainto_tsquery('english', :q) "
                "ORDER BY r DESC, length(term) ASC LIMIT 100"
            ),
            {"q": needle, "ns": f"{ONTOLOGY}:%"},
        ).all()
        if fts:
            top = float(fts[0].r) or 1.0
            for row in fts:
                term = row.term.lower()
                if term == needle:
                    continue
                base = min(0.5 + 0.4 * float(row.r) / top, 0.9)
                if term.startswith(needle):
                    base = max(base, 0.85)
                out.append((row.concept_id, row.term, str(row.term_type), base, True))
        if out:
            return out
        fuzzy = self.session.execute(
            sql_text(
                "SELECT concept_id, term, term_type, similarity(term, :q) AS s "
                "FROM ontology_terms WHERE active AND concept_id LIKE :ns AND term % :q "
                "ORDER BY s DESC LIMIT 50"
            ),
            {"q": needle, "ns": f"{ONTOLOGY}:%"},
        ).all()
        return [
            (
                r.concept_id,
                r.term,
                str(r.term_type),
                0.3 + 0.4 * float(r.s),
                _covers_every_word(needle, r.term),
            )
            for r in fuzzy
        ]

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
