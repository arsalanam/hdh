"""SNOMED CT's OntologyService — the protocol on a DAG (implementation #2).

Hierarchy strategy here is the transitive-closure table: subsumption is
one indexed hit, descendant sweeps are one range scan, and there is no
``path`` to leak — ``ontology_closure`` is PRIVATE to this module
(design snomed-module.md §6–§8). ``attributes()`` is the SNOMED extra
that carries intervention semantics (method / site / morphology…).

``normalize()`` is the design-§7 funnel: FTS over the term index with a
trigram-fuzz fallback (PostgreSQL; exact/substring elsewhere), ranked by
term-match quality, semantic-tag fit to the mention type, and
ancestor-set context — every stage deterministic SQL + scoring, so the
funnel is reproducible offline. SapBERT embeddings slot in later as one
more stage, bench-gated (master doc §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select

from hdh.core.ontology import Candidate, Concept

ONTOLOGY = "snomed_ct"

_TERM_TYPE_RANK = {"preferred": 0, "fsn": 1, "synonym": 2}


def build_service(session: Any) -> SnomedOntologyService:
    """Discovery hook for hdh.core.ontology.get_ontology_service()."""
    return SnomedOntologyService(session)


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_terms"], t["ontology_closure"]


@dataclass
class SnomedOntologyService:
    """The protocol over the closure table (see module docstring)."""

    session: Any
    ontology: ClassVar[str] = ONTOLOGY

    # ── protocol ─────────────────────────────────────────────────────────

    def lookup(self, code: str) -> Concept | None:
        """The concept for an SCTID, or None."""
        row = self._row(code)
        return self._concept(row) if row is not None else None

    def ancestors(self, code: str) -> tuple[Concept, ...]:
        """Every transitive ancestor — one closure join, nearest first."""
        concepts_t, _e, _t, closure_t = _tables()
        rows = self.session.execute(
            select(concepts_t, closure_t.c.min_depth)
            .join(closure_t, closure_t.c.ancestor_id == concepts_t.c.id)
            .where(closure_t.c.descendant_id == self._id(code))
            .order_by(closure_t.c.min_depth, concepts_t.c.code)
        ).all()
        return tuple(self._concept(row) for row in rows)

    def descendants(self, code: str) -> tuple[Concept, ...]:
        """Every transitive descendant — one closure range scan."""
        concepts_t, _e, _t, closure_t = _tables()
        rows = self.session.execute(
            select(concepts_t, closure_t.c.min_depth)
            .join(closure_t, closure_t.c.descendant_id == concepts_t.c.id)
            .where(closure_t.c.ancestor_id == self._id(code))
            .order_by(closure_t.c.min_depth, concepts_t.c.code)
        ).all()
        return tuple(self._concept(row) for row in rows)

    def synonyms(self, code: str) -> tuple[str, ...]:
        """Every active term for the concept: preferred, FSN, synonyms."""
        _c, _e, terms_t, _cl = _tables()
        rows = self.session.execute(
            select(terms_t.c.term, terms_t.c.term_type)
            .where(terms_t.c.concept_id == self._id(code), terms_t.c.active)
            .order_by(terms_t.c.term)
        ).all()
        ranked = sorted(rows, key=lambda r: _TERM_TYPE_RANK.get(str(r.term_type), 9))
        seen: dict[str, None] = {}
        for row in ranked:
            seen.setdefault(row.term, None)
        return tuple(seen)

    def normalize(self, mention: str, context: dict | None = None) -> tuple[Candidate, ...]:
        """The design-§7 funnel, run by :mod:`hdh.core.termsearch`.

        The rungs and the scoring are shared with every other vocabulary —
        they were never SNOMED-specific (design
        rxnorm-and-terminology-boundaries.md §1). What stays here is what
        only this module can compute: semantic-tag fit, and whether a
        candidate sits under an anchor's subtree, which reads the closure
        table this module keeps private.

        ``context`` keys (all optional): ``limit``; ``semantic_tags`` —
        tags the mention type expects (e.g. ["disorder", "finding"]),
        matching candidates rank higher; ``ancestors`` — SCTIDs whose
        subtrees the mention should live under (one closure hit each).
        """
        from hdh.core import termsearch

        return termsearch.search(self.session, self._profile(), mention, context)

    def _profile(self):
        """This vocabulary's shape, for the shared funnel."""
        from hdh.core.termsearch import SearchProfile

        return SearchProfile(
            ontology=ONTOLOGY,
            term_type_rank=_TERM_TYPE_RANK,
            # SNOMED writes abbreviations into the term itself, as
            # "SOB - Shortness of breath": 9,101 active terms in the US
            # Edition follow the shape (issue #54).
            abbreviation_separator=" - ",
            adjust=self._adjust,
        )

    def _adjust(self, concept: Concept, context) -> list[tuple[float, str]]:
        """The boosts only this module can compute."""
        from sqlalchemy import select

        _concepts, _e, _t, closure_t = _tables()
        out: list[tuple[float, str]] = []

        wanted_tags = {t.lower() for t in context.get("semantic_tags", ())}
        if wanted_tags:
            tag = (concept.properties.get("semantic_tag") or "").lower()
            out.append((0.15, f"tag: {tag}") if tag in wanted_tags else (-0.15, ""))

        anchors = [self._id(code) for code in context.get("ancestors", ())]
        if anchors:
            under = self.session.execute(
                select(closure_t.c.min_depth).where(
                    closure_t.c.ancestor_id.in_(anchors),
                    closure_t.c.descendant_id == concept.id,
                )
            ).first()
            if under is not None:
                out.append((0.2, "in context subtree"))
        return out

    def subsumes(self, ancestor_code: str, descendant_code: str) -> bool:
        """One indexed closure hit (strict: a concept never subsumes itself)."""
        _c, _e, _t, closure_t = _tables()
        return (
            self.session.execute(
                select(closure_t.c.min_depth).where(
                    closure_t.c.ancestor_id == self._id(ancestor_code),
                    closure_t.c.descendant_id == self._id(descendant_code),
                )
            ).first()
            is not None
        )

    # ── SNOMED extras ────────────────────────────────────────────────────

    def attributes(self, code: str) -> dict[str, tuple[Concept, ...]]:
        """The concept's defining attributes: {name: (target concepts…)} —
        thrombectomy's method → Removal, site → cerebral artery (design §6)."""
        concepts_t, edges_t, _t, _cl = _tables()
        rows = self.session.execute(
            select(edges_t.c.properties, concepts_t)
            .join(concepts_t, edges_t.c.target_id == concepts_t.c.id)
            .where(edges_t.c.source_id == self._id(code), edges_t.c.edge_type == "attribute")
            .order_by(concepts_t.c.code)
        ).all()
        grouped: dict[str, list[Concept]] = {}
        for row in rows:
            mapping = row._mapping
            name = (mapping["properties"] or {}).get("attribute", {}).get("name", "attribute")
            grouped.setdefault(name, []).append(self._concept(row))
        return {name: tuple(concepts) for name, concepts in grouped.items()}

    def children(self, code: str) -> tuple[Concept, ...]:
        """Direct children only (depth-1 closure rows)."""
        concepts_t, _e, _t, closure_t = _tables()
        rows = self.session.execute(
            select(concepts_t)
            .join(closure_t, closure_t.c.descendant_id == concepts_t.c.id)
            .where(closure_t.c.ancestor_id == self._id(code), closure_t.c.min_depth == 1)
            .order_by(concepts_t.c.code)
        ).all()
        return tuple(self._concept(row) for row in rows)

    # ── private ──────────────────────────────────────────────────────────

    @staticmethod
    def _id(code: str) -> str:
        return f"{ONTOLOGY}:{code.strip()}"

    def _row(self, code: str):
        concepts_t, _e, _t, _cl = _tables()
        return self.session.execute(select(concepts_t).where(concepts_t.c.id == self._id(code))).first()

    def _concept(self, row) -> Concept:
        mapping = row._mapping
        return Concept(
            id=mapping["id"],
            ontology=mapping["ontology"],
            code=mapping["code"],
            display=mapping["display"],
            kind=str(mapping["kind"]),
            properties=dict(mapping["properties"] or {}),
        )
