"""Retrieval over curated clinical knowledge.

Design §6, amended 2026-08-25. The original specified SQLite FTS5 in a
separate ``~/.hdh/knowledge.db``; this module requires **PostgreSQL** and
keeps its chunks in the same database as the chart (ARCHITECTURE §4a).

Two reasons, and neither is taste:

- A knowledge store in its own file cannot be joined against the chart it
  exists to inform. *"Which of my patients have a concern citing this
  chunk?"* stops being a query and becomes a script.
- ``hdh.core.termsearch`` already owns dialect-aware retrieval for four
  vocabularies. A second retrieval mechanism, in a second database, is the
  duplication the RxNorm design was written to prevent.

The retrieval itself is deliberately **not** ``termsearch``: that funnel
ranks short ontology terms against a mention, where covering every word
matters and a trailing clause is an unasked-for claim. This ranks
paragraphs against a clinical situation, where partial overlap is normal
and expected. Same idiom — FTS first, trigram behind it — different
scoring, because the question is different.

**Every hit carries its source and licence.** That is what lets a
generated plan element cite the chunk it came from, and it is the property
that rules out baking this knowledge into weights: retrieval can be
corrected, re-licensed and withdrawn, and a fine-tune cannot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

#: Below this, a trigram hit is noise rather than a weak match.
#:
#: Character trigrams find *some* similarity between any two English
#: paragraphs, so this floor is set from measurement rather than taste.
#: Against the bundled corpus:
#:
#:     "elderly patient on glipizide who lives alone"  →  0.444, 0.203
#:     "orbital mechanics of comets"                   →  0.172
#:
#: 0.15 let the comets through. 0.25 clears the noise and keeps the strong
#: match, at the cost of the 0.203 second hit — which is the right trade
#: here: FTS already returns that chunk, and a weak trigram hit is exactly
#: the confident approximation this project refuses everywhere else.
#:
#: (The floor was also first applied to ``similarity()``, which was the
#: wrong function entirely — see :meth:`PgStore._trigram`.)
TRIGRAM_FLOOR = 0.25

#: Retrieval never returns more than this per call, whatever k asks for —
#: hits become prompt tokens, and an unbounded k is an unbounded bill.
MAX_HITS = 25


@dataclass(frozen=True)
class KnowledgeDoc:
    """One document going in: its text and where it came from."""

    doc_id: str
    text: str
    source: str
    license: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeHit:
    """One chunk coming out, with everything needed to cite it."""

    corpus: str
    doc_id: str
    chunk: str
    score: float
    source: str
    license: str
    metadata: Mapping[str, object]

    def citation(self) -> str:
        """A stable reference to store in ``evidence_refs``."""
        return f"{self.corpus}/{self.doc_id}"


class KnowledgeStore(Protocol):
    """The retrieval contract. One implementation ships; the protocol is
    what lets a vector or hybrid store arrive without touching callers."""

    name: ClassVar[str]

    def search(
        self, query: str, corpus: str, k: int = 5, filters: Mapping | None = None
    ) -> list[KnowledgeHit]:
        """Chunks from one corpus, most relevant first."""
        ...

    def ingest(self, corpus: str, documents: Iterable[KnowledgeDoc]) -> int:
        """Replace a corpus's chunks; returns how many were written."""
        ...


def chunk_document(text: str, *, max_chars: int = 900) -> list[str]:
    """Split on blank lines, then pack paragraphs up to ``max_chars``.

    Paragraph boundaries are respected rather than cut at a character
    count, because a retrieved half-sentence is worse than a slightly long
    chunk: the plan element that cites it has to stand on what the chunk
    actually says.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table():
    from hdh.core.models import Base

    return Base.metadata.tables["knowledge_chunks"]


class PgStore:
    """PostgreSQL full-text search with a trigram fallback."""

    name: ClassVar[str] = "postgres"

    def __init__(self, session) -> None:
        self._session = session

    def ingest(self, corpus: str, documents: Iterable[KnowledgeDoc]) -> int:
        """Replace this corpus wholesale, then insert.

        Delete-then-insert rather than an upsert, because it is idempotent
        for the case that actually happens: a corpus edited in the repo and
        re-ingested. A document removed from the corpus disappears from the
        store, which an upsert would silently leave behind.
        """
        from datetime import datetime

        from sqlalchemy import delete, insert

        table = _table()
        self._session.execute(delete(table).where(table.c.corpus == corpus))
        now = datetime.utcnow()
        rows = [
            {
                "corpus": corpus,
                "doc_id": doc.doc_id,
                "chunk_index": index,
                "text": chunk,
                "source": doc.source,
                "license": doc.license,
                "chunk_metadata": dict(doc.metadata),
                "content_hash": _hash(chunk),
                "ingested_at": now,
            }
            for doc in documents
            for index, chunk in enumerate(chunk_document(doc.text))
        ]
        if rows:
            self._session.execute(insert(table), rows)
        self._session.flush()
        return len(rows)

    def search(
        self, query: str, corpus: str, k: int = 5, filters: Mapping | None = None
    ) -> list[KnowledgeHit]:
        """Chunks from ``corpus``, most relevant first.

        FTS first because it understands word order and stemming; trigram
        behind it because a clinical situation is rarely phrased the way a
        reference statement is. A query matching nothing returns nothing —
        an empty list is a legitimate answer, and the caller is expected to
        say so rather than proceed without evidence.
        """
        from hdh.core.dialect import require_postgresql

        require_postgresql(self._session, "Care-plan knowledge retrieval")

        needle = (query or "").strip()
        if not needle:
            return []
        limit = max(1, min(int(k), MAX_HITS))
        hits = self._fts(needle, corpus, limit, filters)
        if len(hits) < limit:
            seen = {(h.doc_id, h.chunk) for h in hits}
            hits.extend(
                hit
                for hit in self._trigram(needle, corpus, limit, filters)
                if (hit.doc_id, hit.chunk) not in seen
            )
        return hits[:limit]

    # ── the two rungs ────────────────────────────────────────────────

    def _fts(self, needle: str, corpus: str, limit: int, filters: Mapping | None) -> list[KnowledgeHit]:
        from sqlalchemy import text as sql_text

        """Full-text search, with the query's terms OR-ed rather than AND-ed.

        ``plainto_tsquery`` and ``websearch_to_tsquery`` both AND every
        term, which is right for a search box and wrong here: a clinical
        situation shares *some* vocabulary with a reference statement and
        never all of it. Measured on the design's §12 scenario — "elderly
        patient on glipizide who lives alone" against a corpus that says
        *sulfonylurea*, *older adults* and *living alone* — the AND form
        returned nothing at all, because "glipizide" appears nowhere.

        The lexemes are extracted by PostgreSQL rather than split in
        Python, so stemming and stopwords are its rules and the query text
        stays a bound parameter.
        """
        clause, params = self._filter_clause(filters)
        rows = self._session.execute(
            sql_text(
                "WITH q AS ("
                "  SELECT to_tsquery('english', array_to_string("
                "    tsvector_to_array(to_tsvector('english', :q)), ' | ')) AS query"
                ") "
                "SELECT corpus, doc_id, text, source, license, chunk_metadata, "
                "       ts_rank(to_tsvector('english', text), q.query) AS score "
                "FROM knowledge_chunks, q "
                "WHERE corpus = :corpus "
                f"  {clause}"
                "  AND to_tsvector('english', text) @@ q.query "
                "ORDER BY score DESC LIMIT :limit"
            ),
            {"q": needle, "corpus": corpus, "limit": limit, **params},
        ).all()
        return [self._hit(row, float(row.score)) for row in rows]

    def _trigram(self, needle: str, corpus: str, limit: int, filters: Mapping | None) -> list[KnowledgeHit]:
        from sqlalchemy import text as sql_text

        """Trigram fallback, using ``word_similarity`` rather than ``similarity``.

        ``similarity()`` compares two strings whole, so it is dominated by
        length: a 45-character query against a 900-character paragraph
        scores near zero however well it matches. On the §12 scenario it
        peaked at **0.09** — below any useful floor — while
        ``word_similarity(query, text)``, which finds the best-matching
        word sequence *inside* the text, scored **0.44** on the right
        chunk. Same data, same query, right answer only from the second.
        """
        clause, params = self._filter_clause(filters)
        rows = self._session.execute(
            sql_text(
                "SELECT corpus, doc_id, text, source, license, chunk_metadata, "
                "       word_similarity(:q, text) AS score "
                "FROM knowledge_chunks "
                "WHERE corpus = :corpus "
                f"  {clause}"
                "  AND word_similarity(:q, text) > :floor "
                "ORDER BY score DESC LIMIT :limit"
            ),
            {"q": needle, "corpus": corpus, "limit": limit, "floor": TRIGRAM_FLOOR, **params},
        ).all()
        return [self._hit(row, float(row.score)) for row in rows]

    @staticmethod
    def _filter_clause(filters: Mapping | None) -> tuple[str, dict]:
        """Metadata equality filters as SQL, parameterised.

        Keys are bound as parameters too, never interpolated — a filter key
        reaching the query text would be an injection point in the one
        place this codebase builds SQL by hand.
        """
        if not filters:
            return "", {}
        parts, params = [], {}
        for i, (key, value) in enumerate(sorted(filters.items())):
            parts.append(f"AND chunk_metadata ->> :fkey{i} = :fval{i}")
            params[f"fkey{i}"] = str(key)
            params[f"fval{i}"] = str(value)
        return " ".join(parts) + " ", params

    @staticmethod
    def _hit(row, score: float) -> KnowledgeHit:
        return KnowledgeHit(
            corpus=row.corpus,
            doc_id=row.doc_id,
            chunk=row.text,
            score=round(score, 4),
            source=row.source,
            license=row.license,
            metadata=row.chunk_metadata or {},
        )


def corpora(session) -> Sequence[tuple[str, int]]:
    """What is ingested, and how much of it — ``(corpus, chunks)``."""
    from sqlalchemy import func, select

    table = _table()
    return [
        (row.corpus, row.n)
        for row in session.execute(
            select(table.c.corpus, func.count().label("n")).group_by(table.c.corpus).order_by(table.c.corpus)
        ).all()
    ]
