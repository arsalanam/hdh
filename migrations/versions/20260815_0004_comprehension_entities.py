"""comprehension: NoteRecord / NoteMention entities (milestone A).

New TABLES are created by ``get_engine``'s create_all on any database
(stamped or not), so this revision is an inspector-guarded no-op there —
it exists to honor the rule that every schema change ships its own
revision (CONTRIBUTING, issue #30) and to create the tables on databases
migrated exclusively through Alembic.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15
"""

from alembic import op
from sqlalchemy import inspect

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = ("note_records", "note_mentions")


def upgrade() -> None:
    """Create the comprehension tables if create_all has not already."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in TABLES:
        if not inspector.has_table(name):
            Base.metadata.tables[name].create(bind)


def downgrade() -> None:
    """Drop the comprehension tables (reverse creation order)."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in reversed(TABLES):
        if inspector.has_table(name):
            Base.metadata.tables[name].drop(bind)
