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

from sqlalchemy import text as sql_text

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

#: Below this cosine similarity, a vector hit is not about the query.
#:
#: Measured against the bundled corpus with Titan v2, five clinical queries
#: and five deliberately unrelated ones:
#:
#:     worst relevant  0.275  "patient bleeds easily on blood thinners"
#:     best nonsense   0.086  "how to bake sourdough bread at home"
#:
#: No overlap, and a gap of 0.189 to put a floor in. 0.15 is the middle of
#: it rather than the edge of either.
#:
#: The comparison that matters is with the lexical arm, whose scores do NOT
#: separate: nonsense scored 0.020 and the design's own §12 scenario 0.041,
#: a ratio of two, and on "patient bleeds easily on blood thinners" lexical
#: ranked the WRONG document first. Vector separates by a factor of three
#: and ranks correctly.
#:
#: Like TRIGRAM_FLOOR, this is calibrated against a corpus and will go stale
#: as that corpus grows — TRIGRAM_FLOOR already did, which is how the
#: "orbital mechanics" probe started matching. Re-measure when the corpus
#: changes materially rather than trusting the number.
VECTOR_FLOOR = 0.15

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
        from sqlalchemy.exc import ProgrammingError

        from hdh.core.dialect import DatabaseFeatureError

        clause, params = self._filter_clause(filters)
        try:
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
        except ProgrammingError as err:
            # A database that has the tables but not the extension is a
            # normal state — `create_all` builds the schema and cannot
            # install pg_trgm. The raw error names a missing function and
            # not the fix, which cost real time the first time a fresh
            # eval database hit it.
            if "word_similarity" not in str(err):
                raise
            self._session.rollback()
            raise DatabaseFeatureError(
                "trigram retrieval needs the pg_trgm extension, which this database does not "
                "have. Run `CREATE EXTENSION IF NOT EXISTS pg_trgm;` against it, or apply the "
                "migrations (0011 installs it)."
            ) from None
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


class VectorStore:
    """Semantic retrieval: pgvector for storage, an embedder for meaning (#100).

    The argument for it is measured, not aesthetic. Lexical retrieval
    matches character trigrams and word stems, so it returns
    `nsaid-bleeding-risk` for the query "orbital mechanics of comets" —
    PostgreSQL stems *mechanics* and *mechanism* to one root, and the
    document explains harm in terms of mechanisms. The hit is lexically
    genuine and semantically empty.

    That is the shape of the failure the cohort keeps scoring: traceability
    governs 21 of 24 verdicts while *zero* elements are uncited, so plans
    fail on citations that share a word with the claim rather than
    supporting it. No threshold fixes a real lexical hit. Reading meaning
    might.

    **The embedding column lives outside the schema registry**, added by
    migration 0017 and by :meth:`ensure_schema`. Putting a `vector` type in
    the registry would make `pgvector` a mandatory import for every hdh
    install, including the ones that never leave SQLite — and lexical
    retrieval, the default, needs neither the extension nor an AWS account.
    """

    name: ClassVar[str] = "vector"

    def __init__(self, session, embedder=None) -> None:
        self._session = session
        self._embedder = embedder

    @property
    def embedder(self):
        if self._embedder is None:
            from hdh.modules.careplan.embeddings import build_embedder

            self._embedder = build_embedder()
        return self._embedder

    def _require_pgvector(self) -> None:
        from hdh.core.dialect import DatabaseFeatureError, require_postgresql

        require_postgresql(self._session, "Care-plan semantic retrieval")
        installed = self._session.execute(
            sql_text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        if not installed:
            raise DatabaseFeatureError(
                "semantic retrieval needs the pgvector extension, which this database "
                "does not have. Run `CREATE EXTENSION vector;` against it, or start the "
                "containers with `just deps` — the bundled image ships it."
            )

    def ensure_schema(self) -> None:
        """Make the embedding column exist. Idempotent."""
        from hdh.modules.careplan.embeddings import DIMENSIONS

        self._require_pgvector()
        self._session.execute(
            sql_text(f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({DIMENSIONS})")
        )
        self._session.flush()

    def ingest(self, corpus: str, documents: Iterable[KnowledgeDoc]) -> int:
        """Write the chunks, then embed them.

        The text lands through :class:`PgStore` so both stores hold exactly
        the same rows — a corpus that reads differently depending on which
        retriever ingested it would make every comparison between them
        meaningless.
        """
        self.ensure_schema()
        written = PgStore(self._session).ingest(corpus, documents)
        if not written:
            return 0

        rows = self._session.execute(
            sql_text("SELECT id, text FROM knowledge_chunks WHERE corpus = :corpus ORDER BY id"),
            {"corpus": corpus},
        ).all()
        vectors = self.embedder.embed([row.text for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            self._session.execute(
                sql_text("UPDATE knowledge_chunks SET embedding = :v WHERE id = :id"),
                {"v": "[" + ",".join(f"{f:.7f}" for f in vector) + "]", "id": row.id},
            )
        self._session.flush()
        return written

    def search(
        self, query: str, corpus: str, k: int = 5, filters: Mapping | None = None
    ) -> list[KnowledgeHit]:
        """The k nearest chunks by cosine distance.

        Returns nothing for an empty query rather than the k arbitrary
        chunks nearest to a meaningless vector — the same refusal the
        lexical store makes, and for the same reason: an element with no
        retrieved evidence should not be generated at all.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        self._require_pgvector()

        embedded = self.embedder.embed([needle])[0]
        literal = "[" + ",".join(f"{f:.7f}" for f in embedded) + "]"
        clause, params = PgStore(self._session)._filter_clause(filters)
        rows = self._session.execute(
            sql_text(
                "SELECT corpus, doc_id, text, source, license, chunk_metadata, "
                "       1 - (embedding <=> CAST(:v AS vector)) AS score "
                "FROM knowledge_chunks "
                "WHERE corpus = :corpus AND embedding IS NOT NULL "
                f"  {clause}"
                "ORDER BY embedding <=> CAST(:v AS vector) "
                "LIMIT :limit"
            ),
            {"v": literal, "corpus": corpus, "limit": min(k, MAX_HITS), **params},
        ).all()
        return [
            PgStore(self._session)._hit(row, float(row.score))
            for row in rows
            if float(row.score) >= VECTOR_FLOOR
        ]
