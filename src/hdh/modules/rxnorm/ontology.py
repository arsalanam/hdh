"""RxNorm's OntologyService — implementation #4, and the first with no
funnel of its own.

That is the point of milestones M1 and M2: the rungs, the scoring, the
coverage rule and the ceilings live in :mod:`hdh.core.termsearch`, and
this module supplies a :class:`SearchProfile` describing what is
particular about drugs. Everything below is either the graph or the
profile — there is no retrieval code here at all.

**Hierarchy is the specificity ladder.** RxNorm is a graph rather than a
tree, but one chain through it behaves exactly like subsumption:

    Blorbizide  (IN)  →  Blorbizide 10 MG  (SCDC)
                      →  Blorbizide 10 MG Oral Tablet  (SCD)
                      →  Zorbex 10 MG Oral Tablet  (SBD)

so ``ancestors(SBD)`` answers "what is this, more generally", which is
what a reconciliation needs: a patient on Zorbex is on blorbizide, and
nothing else about the graph says so as plainly.

**The term type is a first-class ranking signal**, which is what makes
this profile different from SNOMED's and LOINC's. "Lisinopril" should
prefer the ingredient; "Lisinopril 10 MG Oral Tablet" should prefer the
clinical drug. Left alone, a lexical funnel ranks them by string length
and gets it backwards half the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select

from hdh.core.ontology import Candidate, Concept

ONTOLOGY = "rxnorm"

#: How the funnel breaks ties between terms of one concept.
_TERM_TYPE_RANK = {"preferred": 0, "synonym": 1}

#: The specificity ladder, general → specific. Used for the default
#: preference when a caller does not say which level it wants: a bare drug
#: name most often means the ingredient, which is the level a problem list
#: and an allergy list both work at.
LADDER = ("IN", "PIN", "MIN", "BN", "SCDC", "SCD", "SBD")

#: Boost applied to a candidate whose term type the caller asked for.
#:
#: Larger than half ``termsearch.ELABORATION_PENALTY``, and deliberately: a
#: deeper level is ALWAYS a longer name ("Blorbizide" -> "Blorbizide 10 MG
#: Oral Tablet"), so the elaboration penalty is guaranteed to push against
#: exactly the candidate an explicit level hint asks for. The hint is
#: information the caller has and the funnel does not; the penalty is a prior
#: for when nobody said. Information outranks a prior, so the swing this
#: applies (2x) has to clear the penalty's maximum.
_LEVEL_BOOST = 0.35


def build_service(session: Any) -> RxNormOntologyService:
    """Discovery hook for hdh.core.ontology.get_ontology_service()."""
    return RxNormOntologyService(session)


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"]


@dataclass
class RxNormOntologyService:
    """The protocol over the drug graph (see module docstring)."""

    session: Any
    ontology: ClassVar[str] = ONTOLOGY

    # ── protocol ─────────────────────────────────────────────────────────

    def lookup(self, code: str) -> Concept | None:
        row = self._row(code)
        return self._concept(row) if row is not None else None

    def ancestors(self, code: str) -> tuple[Concept, ...]:
        """Everything this drug is, more generally — nearest first."""
        return tuple(self._walk(code, up=True))

    def descendants(self, code: str) -> tuple[Concept, ...]:
        """Every more specific drug built from this one."""
        return tuple(self._walk(code, up=False))

    def subsumes(self, ancestor_code: str, descendant_code: str) -> bool:
        """True if the ladder runs from the ancestor down to the descendant."""
        if ancestor_code == descendant_code:
            return False  # strict, as the protocol says
        return any(c.code == ancestor_code for c in self.ancestors(descendant_code))

    def synonyms(self, code: str) -> tuple[str, ...]:
        """Every term naming this concept, preferred first."""
        from hdh.core.models import Base

        terms_t = Base.metadata.tables["ontology_terms"]
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
        """Ranked drug candidates, run by :mod:`hdh.core.termsearch`.

        ``context`` keys (all optional): ``limit``; ``levels`` — term types
        the caller wants ranked first (e.g. ``["SCD", "SBD"]`` when a
        strength and a form are known, ``["IN"]`` when only a name is).
        """
        from hdh.core import termsearch

        return termsearch.search(self.session, self._profile(), mention, context)

    # ── the profile: what is particular about drugs ──────────────────────

    def _profile(self):
        """This vocabulary's shape, for the shared funnel.

        No abbreviation separator: RxNorm does not write "ABBR - Expansion"
        terms. Its abbreviations arrive as ordinary source atoms, which the
        loader keeps as synonyms.
        """
        from hdh.core.termsearch import SearchProfile

        return SearchProfile(
            ontology=ONTOLOGY,
            term_type_rank=_TERM_TYPE_RANK,
            abbreviation_separator=None,
            adjust=self._adjust,
        )

    def _adjust(self, concept: Concept, context) -> list[tuple[float, str]]:
        """Prefer the level the caller asked for.

        This is the RxNorm-specific ranking signal, and the reason the
        compositional walk in §5 works: comprehension knows whether it
        extracted a strength and a form, so it can say which level the
        evidence supports instead of leaving a lexical funnel to guess.
        """
        wanted = {str(level).upper() for level in context.get("levels", ())}
        if not wanted:
            return []
        tty = (concept.properties.get("tty") or "").upper()
        if tty in wanted:
            return [(_LEVEL_BOOST, f"level: {tty}")]
        return [(-_LEVEL_BOOST, "")]

    # ── the graph ────────────────────────────────────────────────────────

    def _walk(self, code: str, up: bool) -> list[Concept]:
        """Follow parent_of edges, nearest first, without revisiting.

        Breadth-first because a combination product has SEVERAL parents —
        two ingredients, not one — and a depth-first walk would report them
        in an order that implies a precedence the graph does not have.
        """
        concepts_t, edges_t = _tables()
        start = f"{ONTOLOGY}:{code}"
        seen: set[str] = {start}
        frontier: list[str] = [start]
        out: list[Concept] = []
        while frontier:
            if up:
                rows = self.session.execute(
                    select(edges_t.c.source_id).where(
                        edges_t.c.edge_type == "parent_of", edges_t.c.target_id.in_(frontier)
                    )
                ).all()
            else:
                rows = self.session.execute(
                    select(edges_t.c.target_id).where(
                        edges_t.c.edge_type == "parent_of", edges_t.c.source_id.in_(frontier)
                    )
                ).all()
            frontier = sorted({r[0] for r in rows} - seen)
            seen.update(frontier)
            if frontier:
                found = self.session.execute(select(concepts_t).where(concepts_t.c.id.in_(frontier))).all()
                by_id = {r.id: self._concept(r) for r in found}
                out.extend(by_id[i] for i in frontier if i in by_id)
        return out

    # ── RxNorm extras: the questions the graph makes possible ────────────

    def ingredients_of(self, code: str) -> tuple[Concept, ...]:
        """The ingredients this drug contains.

        A reconciliation question rather than a search one: a patient on
        Zorbamet is on blorbizide AND quixamet, and charting a second
        metformin beside a combination product is how an ingredient gets
        doubled without ever being named twice (design §10 Scenario C).
        """
        return tuple(c for c in self.ancestors(code) if (c.properties.get("tty") or "") in ("IN", "PIN"))

    def brands_of(self, code: str) -> tuple[Concept, ...]:
        """The branded drugs built from this clinical drug."""
        return tuple(c for c in self.descendants(code) if (c.properties.get("tty") or "") == "SBD")

    def attributes(self, code: str) -> dict[str, tuple[Concept, ...]]:
        """Typed relations: dose form, precise ingredient, pack contents."""
        concepts_t, edges_t = _tables()
        rows = self.session.execute(
            select(edges_t.c.properties, concepts_t)
            .join(concepts_t, edges_t.c.target_id == concepts_t.c.id)
            .where(edges_t.c.source_id == f"{ONTOLOGY}:{code}", edges_t.c.edge_type == "attribute")
            .order_by(concepts_t.c.code)
        ).all()
        grouped: dict[str, list[Concept]] = {}
        for row in rows:
            mapping = row._mapping
            rela = (mapping["properties"] or {}).get("rela", "attribute")
            grouped.setdefault(rela, []).append(self._concept(row))
        return {rela: tuple(concepts) for rela, concepts in grouped.items()}

    # ── internals ────────────────────────────────────────────────────────

    def _row(self, code: str):
        concepts_t, _edges = _tables()
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
