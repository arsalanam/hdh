"""ICD-10-CM's OntologyService — implementation #1 of the protocol.

The materialized ``path`` tree (benched, earned — design icd10cm §5) is
THIS module's private strategy: every path/hierarchy_depth touch lives
behind the protocol here or in this module's siblings. Path segments are
dot-free (code dots stripped at load), so ``"19.S50-S59.S52.S520"``
splits cleanly; a NULL path on a row of another ontology raises
:class:`~hdh.core.ontology.UnsupportedHierarchy` instead of silently
matching nothing (design notes-comprehension-service.md §5 item 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import or_, select

from hdh.core.ontology import Candidate, Concept, UnsupportedHierarchy

ONTOLOGY = "icd10cm"


def build_service(session: Any) -> Icd10OntologyService:
    """Discovery hook for hdh.core.ontology.get_ontology_service()."""
    return Icd10OntologyService(session)


def _tables():
    from hdh.core.models import Base

    return Base.metadata.tables["ontology_concepts"]


@dataclass
class Icd10OntologyService:
    """The protocol over the ICD-10-CM path tree (see module docstring)."""

    session: Any
    ontology: ClassVar[str] = ONTOLOGY

    # ── protocol ─────────────────────────────────────────────────────────

    def lookup(self, code: str) -> Concept | None:
        """The concept for a dotted ICD-10-CM code, or None."""
        row = self._row(code)
        return self._concept(row) if row is not None else None

    def ancestors(self, code: str) -> tuple[Concept, ...]:
        """Chapter→…→parent chain as a set (tree: the degenerate DAG case)."""
        path = self._path(code)
        segments = path.split(".")
        prefixes = [".".join(segments[:i]) for i in range(1, len(segments))]
        if not prefixes:
            return ()
        concepts_t = _tables()
        rows = self.session.execute(
            select(concepts_t).where(concepts_t.c.ontology == ONTOLOGY, concepts_t.c.path.in_(prefixes))
        ).all()
        by_path = {row.path: row for row in rows}
        return tuple(self._concept(by_path[p]) for p in prefixes if p in by_path)

    def descendants(self, code: str) -> tuple[Concept, ...]:
        """Everything under the code's subtree (excluding itself)."""
        path = self._path(code)
        concepts_t = _tables()
        rows = self.session.execute(
            select(concepts_t)
            .where(concepts_t.c.ontology == ONTOLOGY, concepts_t.c.path.like(path + ".%"))
            .order_by(concepts_t.c.path)
        ).all()
        return tuple(self._concept(row) for row in rows)

    def synonyms(self, code: str) -> tuple[str, ...]:
        """Terms from the shared index (``hdh icd terms`` / TermsStage,
        snomed design Q2), preferred first; falls back to the concept's
        display columns when the index hasn't been backfilled."""
        row = self._row(code)
        if row is None:
            return ()
        from hdh.core.models import Base

        terms_t = Base.metadata.tables["ontology_terms"]
        rows = self.session.execute(
            select(terms_t.c.term, terms_t.c.term_type)
            .where(terms_t.c.concept_id == row.id, terms_t.c.active)
            .order_by(terms_t.c.term)
        ).all()
        if rows:
            rank = {"preferred": 0, "fsn": 1, "synonym": 2}
            seen: dict[str, None] = {}
            for term_row in sorted(rows, key=lambda r: rank.get(str(r.term_type), 9)):
                seen.setdefault(term_row.term, None)
            return tuple(seen)
        terms = [row.display]
        if row.short_display and row.short_display != row.display:
            terms.append(row.short_display)
        return tuple(terms)

    def normalize(self, mention: str, context: dict | None = None) -> tuple[Candidate, ...]:
        """Ranked candidates via the catalog search (FTS/trigram on PG).

        The axis-aware codify() funnel remains this module's richer
        entry point; normalize() is the protocol's plain-mention shape.
        """
        from hdh.modules.icd10cm.cli import search_concepts

        limit = int((context or {}).get("limit", 10))
        hits = search_concepts(self.session, mention, limit)
        out = []
        for rank, hit in enumerate(hits):
            concept = self.lookup(hit.code)
            if concept is not None:
                out.append(
                    Candidate(concept=concept, score=1.0 - rank / max(len(hits), 1), reason="catalog search")
                )
        return tuple(out)

    def subsumes(self, ancestor_code: str, descendant_code: str) -> bool:
        """One prefix test on the materialized paths."""
        ancestor = self._path(ancestor_code)
        descendant = self._path(descendant_code)
        return descendant.startswith(ancestor + ".")

    # ── private: the path strategy ───────────────────────────────────────

    def _row(self, code: str):
        concepts_t = _tables()
        return self.session.execute(
            select(concepts_t).where(concepts_t.c.ontology == ONTOLOGY, concepts_t.c.code == code.upper())
        ).first()

    def _path(self, code: str) -> str:
        row = self._row(code)
        if row is None:
            raise LookupError(f"ICD-10-CM code '{code}' not in the loaded catalog")
        if not row.path:
            raise UnsupportedHierarchy(
                f"concept {row.id} has no materialized path — tree helpers serve only the {ONTOLOGY} ontology"
            )
        return row.path

    def _concept(self, row) -> Concept:
        return Concept(
            id=row.id,
            ontology=row.ontology,
            code=row.code,
            display=row.display,
            kind=str(row.kind),
            properties=dict(row.properties or {}),
        )


def descendant_ids(session: Any, ids: list[str]) -> list[str]:
    """Subtree ids for anchor ids, self included — the pattern compiler's
    bulk entry point, kept HERE so the path strategy stays module-private
    (patterns._descend dispatches to this; design §5 item 1)."""
    concepts_t = _tables()
    rows = session.execute(select(concepts_t.c.path).where(concepts_t.c.id.in_(ids))).all()
    paths = [row.path for row in rows]
    if any(not p for p in paths):
        raise UnsupportedHierarchy(
            "pattern anchors include concepts without a materialized path — "
            f"tree descent serves only the {ONTOLOGY} ontology"
        )
    if not paths:
        return []
    prefix_filters = [or_(concepts_t.c.path == p, concepts_t.c.path.like(p + ".%")) for p in paths]
    return [
        row[0] for row in session.execute(select(concepts_t.c.id).where(or_(*prefix_filters)).limit(5000))
    ]
