"""The one lexical funnel, shared by every vocabulary module.

Search strategy is not a property of a vocabulary. Everything issue #54
measured — exact-term-first, the ``ABBR - Expansion`` alias, the per-word
coverage cap, "a fuzzy match is a guess about the words it did not cover"
— is about lexical retrieval, and it applied unchanged when LOINC arrived.
Written per module it gets copied per module: the LOINC funnel ended up
importing SNOMED's private ``_covers_every_word``, a module reaching into
another module's internals against the rule in ``hdh/modules/__init__.py``
(design rxnorm-and-terminology-boundaries.md §1).

So the split is:

- **here** — the rungs, the scoring, the coverage rule, the ceilings. One
  place to fix a ranking bug; one place to add a rung.
- **the module** — a :class:`SearchProfile` saying what its vocabulary
  needs: how its term types rank, whether it spells abbreviations out, and
  an ``adjust`` hook for the boosts only it can compute. SNOMED's subtree
  check reads the closure table, which is module-private and must stay
  that way, so it arrives as a callback rather than as configuration.

``OntologyService.normalize()`` keeps its shape — it delegates here — so
consumers see no change at all.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hdh.core.ontology import Candidate, Concept

#: Ceiling for a match that explains only PART of the mention. Deliberately
#: below the comprehension pipeline's 0.6 review threshold: such a match is
#: a guess about the words it could not account for, so it should reach a
#: human rather than a chart (issue #54, where 'Bronze diabetes' was
#: charted for "sugar diabetes" at 0.61).
PARTIAL_COVERAGE_CEILING = 0.55

#: How close a mention's word must be to some word of the term before we
#: call it accounted for. Measured on the SNOMED frontier corpus: typos sit
#: at 0.75–0.96 ("hypertenison"/'Hypertensive' 0.75) while lay phrasing
#: bottoms out at 0.00–0.55 ("lung" against 'Smoker'), so the gap is wide.
FUZZY_WORD_MATCH = 0.7

#: An abbreviation short enough to be one. Restricting to alphanumerics
#: also keeps the LIKE pattern literal — no escaping of % or _.
ABBREVIATION_LENGTH = range(2, 11)


@dataclass(frozen=True)
class SearchProfile:
    """What one vocabulary needs the shared funnel to know about it.

    ``adjust`` is the escape hatch for boosts a module alone can compute —
    SNOMED's semantic tag and ancestor subtree, LOINC's specimen axis. It
    receives the candidate concept and the caller's context, and returns
    ``(delta, reason)`` pairs. Everything a module CAN say declaratively it
    says in the fields, so that the hook stays small.
    """

    ontology: str
    term_type_rank: Mapping[str, int] = field(default_factory=lambda: {"preferred": 0, "synonym": 1})
    #: Separator in "ABBR - Expansion" terms, or None where the vocabulary
    #: does not spell abbreviations out that way (LOINC keeps them as
    #: ordinary synonyms in RELATEDNAMES2).
    abbreviation_separator: str | None = None
    partial_coverage_ceiling: float = PARTIAL_COVERAGE_CEILING
    adjust: Callable[[Concept, Mapping[str, Any]], Sequence[tuple[float, str]]] | None = None


def _words(text: str) -> list[str]:
    """Words worth matching on — two-character fragments carry no signal."""
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 2]


def covers_every_word(mention: str, term: str) -> bool:
    """Does the term account for EVERY word of the mention?

    This is the difference between a typo and a lay phrase, and only the
    trigram rung needs it: an FTS hit already matched every lexeme, while
    trigram scores whole strings and will happily return 'Smoker' for
    "smoker's lung" — a confident answer to half the question (issue #54).

    Similarity alone cannot make the call: measured on the frontier corpus,
    typos span 0.35–0.71 and wrong answers 0.33–0.56, which overlap. Per
    word they separate cleanly.
    """
    term_words = _words(term)
    if not term_words:
        return False
    return all(
        max((difflib.SequenceMatcher(None, word, tw).ratio() for tw in term_words), default=0.0)
        >= FUZZY_WORD_MATCH
        for word in _words(mention)
    )


def looks_like_abbreviation(needle: str) -> bool:
    """Could this mention be an abbreviation a vocabulary spells out?

    Anything with a space is a phrase and anything longer than ten
    characters is a word — neither can head an ``ABBR - Expansion`` term,
    so querying for them buys an index scan that cannot hit.
    """
    return len(needle) in ABBREVIATION_LENGTH and needle.isalnum()


def _tables():
    from hdh.core.models import Base

    return Base.metadata.tables["ontology_concepts"], Base.metadata.tables["ontology_terms"]


def _concept(row) -> Concept:
    mapping = row._mapping
    return Concept(
        id=mapping["id"],
        ontology=mapping["ontology"],
        code=mapping["code"],
        display=mapping["display"],
        kind=mapping["kind"],
        properties=mapping["properties"] or {},
    )


# ── the rungs ────────────────────────────────────────────────────────────


def _abbreviation_rows(session, profile: SearchProfile, needle: str):
    """Terms of the form ``ABBR - Expansion`` whose abbreviation is the
    mention — SNOMED carries 9,101 of them, so the terminology already
    ships the abbreviation table we would otherwise curate.

    The expansion decides how strong the claim is: one abbreviation can
    head several terms, and only an expansion the concept ALSO goes by is a
    true alias. "MI" heads five terms, and without that test the tie fell
    to concept-code order and landed on 'Coronary thrombosis NOT resulting
    in myocardial infarction'.

    The FTS predicate is what makes it affordable: an abbreviation is
    always a lexeme of the term spelling it out, so the GIN index narrows
    a million rows before the LIKE runs — 580ms against 1.2ms.
    """
    from sqlalchemy import text as sql_text

    separator = profile.abbreviation_separator
    if separator is None or not looks_like_abbreviation(needle):
        return []
    rows = session.execute(
        sql_text(
            "SELECT t.concept_id, t.term, t.term_type, EXISTS ("
            "    SELECT 1 FROM ontology_terms o"
            "     WHERE o.concept_id = t.concept_id AND o.active"
            "       AND lower(o.term) = lower(substr(t.term, position(:sep in t.term) + :seplen))"
            ") AS is_alias "
            "FROM ontology_terms t "
            "WHERE t.active AND t.concept_id LIKE :ns "
            "  AND to_tsvector('english', t.term) @@ plainto_tsquery('english', :q) "
            "  AND lower(t.term) LIKE :prefix LIMIT 20"
        ),
        {
            "prefix": f"{needle}{separator}%",
            "q": needle,
            "ns": f"{profile.ontology}:%",
            "sep": separator,
            "seplen": len(separator),
        },
    ).all()
    return [(r.concept_id, r.term, str(r.term_type), 1.0 if r.is_alias else 0.9, True) for r in rows]


def _postgres_rows(session, profile: SearchProfile, needle: str):
    """exact → abbreviation → FTS → trigram, in that order and for reasons."""
    from sqlalchemy import text as sql_text

    namespace = f"{profile.ontology}:%"
    # Exact matches FIRST, as their own query: single-word mentions produce
    # hundreds of FTS ties, and LIMIT could otherwise drop the exact term
    # from the pool entirely.
    exact = session.execute(
        sql_text(
            "SELECT concept_id, term, term_type FROM ontology_terms "
            "WHERE active AND concept_id LIKE :ns AND lower(term) = :q LIMIT 20"
        ),
        {"q": needle, "ns": namespace},
    ).all()
    out = [(r.concept_id, r.term, str(r.term_type), 1.0, True) for r in exact]
    out.extend(_abbreviation_rows(session, profile, needle))

    fts = session.execute(
        sql_text(
            "SELECT concept_id, term, term_type, "
            "       ts_rank(to_tsvector('english', term), plainto_tsquery('english', :q), 1) AS r "
            "FROM ontology_terms WHERE active AND concept_id LIKE :ns "
            "AND to_tsvector('english', term) @@ plainto_tsquery('english', :q) "
            "ORDER BY r DESC, length(term) ASC LIMIT 100"
        ),
        {"q": needle, "ns": namespace},
    ).all()
    if fts:
        top = float(fts[0].r) or 1.0
        for row in fts:
            term = row.term.lower()
            if term == needle:
                continue  # already in via the exact query
            # non-exact matches cap BELOW the exact ceiling; shorter terms
            # (closer to the mention) rank higher via the sort
            base = min(0.5 + 0.4 * float(row.r) / top, 0.9)
            if term.startswith(needle):
                base = max(base, 0.85)
            # An FTS hit always covers the mention: plainto_tsquery ANDs its
            # lexemes, so the term matched every one of them.
            out.append((row.concept_id, row.term, str(row.term_type), base, True))
    if out:
        return out

    fuzzy = session.execute(
        sql_text(
            "SELECT concept_id, term, term_type, similarity(term, :q) AS s "
            "FROM ontology_terms WHERE active AND concept_id LIKE :ns AND term % :q "
            "ORDER BY s DESC LIMIT 50"
        ),
        {"q": needle, "ns": namespace},
    ).all()
    return [
        (r.concept_id, r.term, str(r.term_type), 0.3 + 0.4 * float(r.s), covers_every_word(needle, r.term))
        for r in fuzzy
    ]


def _portable_rows(session, profile: SearchProfile, needle: str):
    """Exact / prefix / substring — SQLite and anything else."""
    from sqlalchemy import func, or_, select

    _concepts, terms_t = _tables()
    rows = session.execute(
        select(terms_t.c.concept_id, terms_t.c.term, terms_t.c.term_type)
        .where(
            terms_t.c.active,
            terms_t.c.concept_id.like(f"{profile.ontology}:%"),
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


# ── the funnel ───────────────────────────────────────────────────────────


def search(
    session, profile: SearchProfile, mention: str, context: Mapping[str, Any] | None = None
) -> tuple[Candidate, ...]:
    """Ranked candidates for a free-text mention, in one vocabulary."""
    from sqlalchemy import select

    ctx = dict(context or {})
    limit = int(ctx.get("limit", 10))
    needle = mention.strip().lower()
    if not needle:
        return ()

    dialect = session.get_bind().dialect.name
    rows = (
        _postgres_rows(session, profile, needle)
        if dialect == "postgresql"
        else _portable_rows(session, profile, needle)
    )

    best: dict[str, tuple[float, str, bool]] = {}
    for concept_id, term, term_type, base, covered in rows:
        score = base - 0.01 * profile.term_type_rank.get(term_type, 2)  # type is a tiebreaker
        if score > best.get(concept_id, (-1.0, "", True))[0]:
            best[concept_id] = (score, term, covered)

    concepts_t, _terms = _tables()
    ranked: list[tuple[float, Candidate]] = []
    for concept_id, (score, term, covered) in best.items():
        row = session.execute(select(concepts_t).where(concepts_t.c.id == concept_id)).first()
        if row is None:
            continue
        concept = _concept(row)
        reasons = [f"term: {term}"]
        if profile.adjust is not None:
            for delta, reason in profile.adjust(concept, ctx):
                score += delta
                if reason:
                    reasons.append(reason)
        if not covered:
            # The term explains only part of the mention. Whatever else
            # recommends it — a matching semantic tag, the right subtree —
            # it is still a guess about the part it did not match, so it
            # must not reach chartable confidence.
            score = min(score, profile.partial_coverage_ceiling)
            reasons.append("partial: matches some of the mention")
        # rank on the RAW score (clamping would flatten exact matches into
        # ties with boosted partials); report a clamped score
        ranked.append(
            (score, Candidate(concept=concept, score=round(min(score, 1.0), 3), reason="; ".join(reasons)))
        )
    ranked.sort(key=lambda pair: (-pair[0], pair[1].concept.code))
    return tuple(candidate for _raw, candidate in ranked[:limit])
