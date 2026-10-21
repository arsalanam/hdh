"""comprehension: Dictation entity (media I/O service phase 2).

The ``dictations`` table stores a saved transcript of a recorded encounter or
dictated note (design docs/design/media-io-service.md §3) — a source awaiting
charting, referenced as "the last dictation" to drive "update the chart now".

New TABLES are created by ``get_engine``'s create_all on any database, so this
revision is an inspector-guarded no-op there — it exists to honor the rule that
every schema change ships its own revision (CONTRIBUTING, issue #30) and to
create the table on databases migrated exclusively through Alembic.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-20
"""

from alembic import op
from sqlalchemy import inspect

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

TABLES = ("dictations",)


def upgrade() -> None:
    """Create the dictations table if create_all has not already."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in TABLES:
        if not inspector.has_table(name):
            Base.metadata.tables[name].create(bind)


def downgrade() -> None:
    """Drop the dictations table."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in reversed(TABLES):
        if inspector.has_table(name):
            Base.metadata.tables[name].drop(bind)
