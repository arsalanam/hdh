"""What the care-plan module needs a PostgreSQL database to have.

Kept here rather than in `hdh.core.dbinit` because core does not know this
module exists — the dependency rule the architecture rests on, and one the
first draft of `dbinit` broke by importing this package for a constant.

Core installs what core needs (`pg_trgm`, for `termsearch`). This declares
what semantic retrieval needs, and the CLI composes the two.
"""

from __future__ import annotations

#: Extensions this module owns, and what stops working without each.
EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("vector", "semantic retrieval (careplan `vector` and `vector+rerank`)"),
)


def ensure_embedding_column(session) -> str:
    """Add ``knowledge_chunks.embedding``; returns a line describing what happened.

    Separate from migration 0017 rather than duplicating it: 0017 owns the
    column for databases under Alembic, and this owns it for the fresh path
    that stamps instead of upgrading. Both are ``IF NOT EXISTS``, so a
    database that has been through one is untouched by the other.
    """
    from sqlalchemy import text as sql_text

    from hdh.modules.careplan.embeddings import DIMENSIONS

    has_vector = session.execute(
        sql_text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    if not has_vector:
        return "skipped    knowledge_chunks.embedding — needs the vector extension"

    if session.execute(sql_text("SELECT to_regclass('knowledge_chunks')")).scalar() is None:
        return "skipped    knowledge_chunks.embedding — no knowledge_chunks table yet"

    present = session.execute(
        sql_text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'knowledge_chunks' AND column_name = 'embedding'"
        )
    ).scalar()
    if present:
        return "ok         knowledge_chunks.embedding"

    session.execute(
        sql_text(f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({DIMENSIONS})")
    )
    session.commit()
    return "installed  knowledge_chunks.embedding"
