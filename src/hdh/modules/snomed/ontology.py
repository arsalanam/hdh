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

from sqlalchemy import func, or_, select

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
        """The design-§7 funnel: term search (FTS with progressive
        relaxation → trigram fuzz on PostgreSQL; exact/substring
        elsewhere), then deterministic ranking by term-match quality,
        semantic-tag fit, and ancestor-set context.

        ``context`` keys (all optional): ``limit``; ``semantic_tags`` —
        tags the mention type expects (e.g. ["disorder", "finding"]),
        matching candidates rank higher; ``ancestors`` — SCTIDs whose
        subtrees the mention should live under (one closure hit each).
        """
        ctx = context or {}
        limit = int(ctx.get("limit", 10))
        needle = mention.strip().lower()
        if not needle:
            return ()
        matches = self._search_terms(needle)
        wanted_tags = {t.lower() for t in ctx.get("semantic_tags", ())}
        anchor_ids = [self._id(code) for code in ctx.get("ancestors", ())]
        concepts_t, _e, _t, closure_t = _tables()
        best: dict[str, tuple[float, str]] = {}
        for concept_id, term, term_type, base in matches:
            score = base - 0.01 * _TERM_TYPE_RANK.get(term_type, 2)  # type is a tiebreaker
            if score > best.get(concept_id, (-1.0, ""))[0]:
                best[concept_id] = (score, term)
        ranked: list[tuple[float, Candidate]] = []
        for concept_id, (score, term) in best.items():
            row = self.session.execute(select(concepts_t).where(concepts_t.c.id == concept_id)).first()
            if row is None:
                continue
            concept = self._concept(row)
            reasons = [f"term: {term}"]
            tag = (concept.properties.get("semantic_tag") or "").lower()
            if wanted_tags:
                if tag in wanted_tags:
                    score += 0.15
                    reasons.append(f"tag: {tag}")
                else:
                    score -= 0.15
            if anchor_ids:
                under = self.session.execute(
                    select(closure_t.c.min_depth).where(
                        closure_t.c.ancestor_id.in_(anchor_ids),
                        closure_t.c.descendant_id == concept.id,
                    )
                ).first()
                if under is not None:
                    score += 0.2
                    reasons.append("in context subtree")
            # rank on the RAW score (clamping would flatten exact matches
            # into ties with boosted partials); report a clamped score
            ranked.append(
                (
                    score,
                    Candidate(concept=concept, score=round(min(score, 1.0), 3), reason="; ".join(reasons)),
                )
            )
        ranked.sort(key=lambda pair: (-pair[0], pair[1].concept.code))
        return tuple(candidate for _raw, candidate in ranked[:limit])

    def _search_terms(self, needle: str) -> list[tuple[str, str, str, float]]:
        """Stage 1 of the funnel: (concept_id, term, term_type, base score)
        rows from the term index — FTS then trigram on PostgreSQL (the
        accelerate-stage indexes), exact/prefix/substring elsewhere."""
        _c, _e, terms_t, _cl = _tables()
        if self.session.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text as sql_text

            # Exact term matches FIRST, as their own query: single-word
            # mentions produce hundreds of FTS ties, and LIMIT could
            # otherwise drop the exact term from the pool entirely.
            exact = self.session.execute(
                sql_text(
                    "SELECT concept_id, term, term_type FROM ontology_terms "
                    "WHERE active AND lower(term) = :q LIMIT 20"
                ),
                {"q": needle},
            ).all()
            out = [(row.concept_id, row.term, str(row.term_type), 1.0) for row in exact]
            fts = self.session.execute(
                sql_text(
                    "SELECT concept_id, term, term_type, "
                    "       ts_rank(to_tsvector('english', term), plainto_tsquery('english', :q), 1) AS r "
                    "FROM ontology_terms WHERE active "
                    "AND to_tsvector('english', term) @@ plainto_tsquery('english', :q) "
                    "ORDER BY r DESC, length(term) ASC LIMIT 100"
                ),
                {"q": needle},
            ).all()
            if fts:
                top = float(fts[0].r) or 1.0
                for row in fts:
                    term = row.term.lower()
                    if term == needle:
                        continue  # already in via the exact query
                    # non-exact matches cap BELOW the exact ceiling; shorter
                    # terms (closer to the mention) rank higher via the sort
                    base = min(0.5 + 0.4 * float(row.r) / top, 0.9)
                    if term.startswith(needle):
                        base = max(base, 0.85)
                    out.append((row.concept_id, row.term, str(row.term_type), base))
            if out:
                return out
            fuzzy = self.session.execute(
                sql_text(
                    "SELECT concept_id, term, term_type, similarity(term, :q) AS s "
                    "FROM ontology_terms WHERE active AND term % :q "
                    "ORDER BY s DESC LIMIT 50"
                ),
                {"q": needle},
            ).all()
            return [(row.concept_id, row.term, str(row.term_type), 0.3 + 0.4 * float(row.s)) for row in fuzzy]
        rows = self.session.execute(
            select(terms_t.c.concept_id, terms_t.c.term, terms_t.c.term_type)
            .where(
                terms_t.c.active,
                or_(func.lower(terms_t.c.term) == needle, func.lower(terms_t.c.term).like(f"%{needle}%")),
            )
            .limit(200)
        ).all()
        out = []
        for row in rows:
            term = row.term.lower()
            base = 1.0 if term == needle else 0.7 if term.startswith(needle) else 0.4
            out.append((row.concept_id, row.term, str(row.term_type), base))
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
