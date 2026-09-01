"""knowledge chunks can carry an embedding (#100).

Semantic retrieval needs the vectors beside the text they came from. The
column is `vector(1024)` — Titan v2's default width, pinned in
`careplan/embeddings.py` because a corpus embedded at two widths cannot be
searched as one.

PostgreSQL only, and only where pgvector is installed. Lexical retrieval is
the default and needs neither, so a database without the extension keeps
working — it simply cannot serve the `vector` retriever, which says so.

No index. At 32 chunks a sequential scan is faster than IVFFlat, and an
approximate index built over a corpus this small returns worse results than
no index at all. When the corpus grows past a few thousand chunks this is
the migration to revisit.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-01
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

TABLE = "knowledge_chunks"
COLUMN = "embedding"
DIMENSIONS = 1024


def _columns(inspector) -> set[str]:
    if TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _columns(inspect(bind)) or COLUMN in _columns(inspect(bind)):
        return

    available = bind.execute(
        text("SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'")
    ).scalar()
    if not available:
        # Not an error. The extension is optional infrastructure, and a
        # database without it must not fail to migrate — it just cannot run
        # semantic retrieval, and `VectorStore` says exactly that when asked.
        return

    bind.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} vector({DIMENSIONS})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if COLUMN in _columns(inspect(bind)):
        op.drop_column(TABLE, COLUMN)
