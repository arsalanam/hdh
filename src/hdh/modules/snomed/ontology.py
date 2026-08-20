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

import difflib
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import func, or_, select

from hdh.core.ontology import Candidate, Concept

ONTOLOGY = "snomed_ct"

_TERM_TYPE_RANK = {"preferred": 0, "fsn": 1, "synonym": 2}

#: Ceiling for a fuzzy match that explains only PART of the mention.
#: Deliberately below the comprehension pipeline's 0.6 review threshold: such
#: a match is a guess about the words it could not account for, so it should
#: reach a human rather than a chart. The coupling is intentional — see issue
#: #54, where 'Bronze diabetes' was charted for "sugar diabetes" at 0.61.
PARTIAL_COVERAGE_CEILING = 0.55

#: How close a mention's word must be to some word of the term before we
#: call it accounted for. Measured against the frontier corpus: typos sit at
#: 0.75–0.96 ("hypertenison"/'Hypertensive' 0.75) while lay phrasing bottoms
#: out at 0.00–0.55 ("lung" against 'Smoker'), so the gap is wide.
_FUZZY_WORD_MATCH = 0.7

#: SNOMED's ``ABBR - Expansion`` terms carry 2–10 character abbreviations.
#: Restricting to alphanumerics keeps the LIKE pattern literal (no escaping
#: of % or _) as well as cheap.
_ABBREVIATION_LEN = range(2, 11)


def _looks_like_abbreviation(needle: str) -> bool:
    """Could this mention be an abbreviation SNOMED spells out for us?

    Anything with a space is a phrase, and anything longer than ten
    characters is a word — neither can head an ``ABBR - Expansion`` term, so
    querying for them would buy an index scan that cannot hit.
    """
    return len(needle) in _ABBREVIATION_LEN and needle.isalnum()


def _words(text: str) -> list[str]:
    """Words worth matching on — two-character fragments carry no signal."""
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 2]


def _covers_every_word(mention: str, term: str) -> bool:
    """Does the term account for EVERY word of the mention?

    This is the difference between a typo and a lay phrase, and only the
    trigram rung needs it: an FTS hit already matched every lexeme, while
    trigram scores whole strings and will happily return 'Smoker' for
    "smoker's lung" or 'Bronze diabetes' for "sugar diabetes" — each a
    confident answer to half the question (issue #54).

    Similarity alone cannot make this call: measured on the frontier corpus,
    typos span 0.35–0.71 and wrong answers 0.33–0.56, which overlap. Per
    word they separate cleanly.
    """
    term_words = _words(term)
    if not term_words:
        return False
    return all(
        max((difflib.SequenceMatcher(None, word, tw).ratio() for tw in term_words), default=0.0)
        >= _FUZZY_WORD_MATCH
        for word in _words(mention)
    )


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
        best: dict[str, tuple[float, str, bool]] = {}
        for concept_id, term, term_type, base, covered in matches:
            score = base - 0.01 * _TERM_TYPE_RANK.get(term_type, 2)  # type is a tiebreaker
            if score > best.get(concept_id, (-1.0, "", True))[0]:
                best[concept_id] = (score, term, covered)
        ranked: list[tuple[float, Candidate]] = []
        for concept_id, (score, term, covered) in best.items():
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
            if not covered:
                # The term explains only part of the mention. Whatever else
                # recommends it — a matching semantic tag, the right subtree
                # — it is still a guess about the part it did not match, so
                # it must not reach chartable confidence.
                score = min(score, PARTIAL_COVERAGE_CEILING)
                reasons.append("partial: matches some of the mention")
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

    def _search_terms(self, needle: str) -> list[tuple[str, str, str, float, bool]]:
        """Stage 1 of the funnel: (concept_id, term, term_type, base score,
        covers_whole_mention) rows from the term index — FTS then trigram on
        PostgreSQL (the accelerate-stage indexes), exact/prefix/substring
        elsewhere.

        ``covers_whole_mention`` is False when the term accounts for only
        SOME of a multi-word mention — 'Bronze diabetes' for "sugar
        diabetes", 'Underactive infant' for "underactive thyroid". Such a
        candidate is a guess about the half it did not match, and
        ``normalize`` caps it below the review threshold.
        """
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
            out = [(row.concept_id, row.term, str(row.term_type), 1.0, True) for row in exact]
            out.extend(self._abbreviation_matches(needle))
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
                    # An FTS hit always covers the mention: plainto_tsquery
                    # ANDs its lexemes, so the term matched every one of them.
                    out.append((row.concept_id, row.term, str(row.term_type), base, True))
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
            return [
                (
                    row.concept_id,
                    row.term,
                    str(row.term_type),
                    0.3 + 0.4 * float(row.s),
                    _covers_every_word(needle, row.term),
                )
                for row in fuzzy
            ]
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
            # every row here matched the whole needle, exactly or as a substring
            out.append((row.concept_id, row.term, str(row.term_type), base, True))
        return out

    def _abbreviation_matches(self, needle: str) -> list[tuple[str, str, str, float, bool]]:
        """SNOMED writes abbreviations into the term itself, as
        ``ABBR - Expansion``: 'SOB - Shortness of breath', 'AF - Atrial
        fibrillation', 'COPD - Chronic obstructive pulmonary disease'. 9,101
        active terms in the US Edition follow the shape, so the terminology
        already carries the abbreviation table we would otherwise curate.

        A mention that IS the abbreviation is an exact hit on that alias and
        scores like one. Without this, "SOB" loses to 'Sobbing respiration',
        whose only claim on the mention is that the English stemmer maps
        'sobbing' → 'sob' — a wrong code, charted at 1.00 (issue #54).

        One abbreviation can head several terms, so the expansion decides how
        strong the claim is: an expansion the concept ALSO goes by is a true
        alias, while a longer qualified phrase is a weaker claim on the bare
        abbreviation. "MI" heads five terms, and only that test separates
        'MI - myocardial infarction' (a name 22298006 goes by) from
        'MI - Myocardial infarction aborted', whose concept is actually
        called *Coronary thrombosis NOT resulting in myocardial infarction* —
        very nearly the opposite of what the clinician wrote.
        """
        if not _looks_like_abbreviation(needle):
            return []
        from sqlalchemy import text as sql_text

        # The FTS predicate is what makes this affordable: an abbreviation is
        # always a lexeme of the term that spells it out, so the GIN index
        # narrows 1M rows to a handful before the LIKE runs. Without it the
        # LIKE is a sequential scan — 580ms per mention against 1.2ms.
        # (An abbreviation that stems to a stop word cannot be found this way;
        # none of the 2+ character ones we care about do.)
        rows = self.session.execute(
            sql_text(
                "SELECT t.concept_id, t.term, t.term_type, EXISTS ("
                "    SELECT 1 FROM ontology_terms o"
                "     WHERE o.concept_id = t.concept_id AND o.active"
                "       AND lower(o.term) = lower(substr(t.term, position(' - ' in t.term) + 3))"
                ") AS is_alias "
                "FROM ontology_terms t "
                "WHERE t.active "
                "  AND to_tsvector('english', t.term) @@ plainto_tsquery('english', :q) "
                "  AND lower(t.term) LIKE :prefix LIMIT 20"
            ),
            {"prefix": f"{needle} - %", "q": needle},
        ).all()
        return [
            (row.concept_id, row.term, str(row.term_type), 1.0 if row.is_alias else 0.9, True) for row in rows
        ]

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
