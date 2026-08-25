"""careplan: the knowledge-chunk store (design §6, amended).

One new table. Follows the guarded pattern 0005-0010: ``create_all``
builds it on a fresh database, and this revision covers one that already
exists.

The trigram index is PostgreSQL-only and created separately, because it
needs the pg_trgm extension. Without it retrieval still works — the
similarity() fallback just scans — but on a corpus of any size that is the
difference between milliseconds and a table scan, and termsearch already
learned this lesson the expensive way (580ms to 1.2ms).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-25
"""

from alembic import op
from sqlalchemy import inspect

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLE = "knowledge_chunks"


def upgrade() -> None:
    from hdh.core.models import Base

    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        table = Base.metadata.tables.get(TABLE)
        if table is None:  # the careplan module is not registered in this build
            return
        table.create(bind, checkfirst=True)

    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_text_trgm "
            f"ON {TABLE} USING gin (text gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_text_fts "
            f"ON {TABLE} USING gin (to_tsvector('english', text))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_text_trgm")
        op.execute("DROP INDEX IF EXISTS ix_knowledge_text_fts")
    if inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
