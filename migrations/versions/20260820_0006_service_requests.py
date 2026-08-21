"""service requests: the table, and what the chart was missing (#52).

Additive throughout, and guarded the same way 0005 is. ``create_all``
builds the new table on a fresh database but never ALTERs an existing
one, so the new COLUMNS on ``lab_results`` and ``prescriptions`` have to
be added explicitly — the stale-schema failure class issue #30 was about.

Existing rows keep working: ``request_id IS NULL`` means "recorded before
requests existed", which is honest rather than a backfilled guess.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-20
"""

from alembic import op
from sqlalchemy import inspect

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

REQUESTS_TABLE = "service_requests"

#: table -> columns this revision adds, built fresh per call because
#: Alembic consumes the Column object it is handed.
NEW_COLUMNS = {
    "lab_results": ("request_id", "value_text", "comparator"),
    "prescriptions": ("request_id",),
}

#: PostgreSQL keeps a table's ENUM types after the table is dropped, so a
#: re-upgrade would collide with them on CREATE TYPE unless downgrade
#: removes them too (learned on migration 0005).
ENUM_TYPES = ("servicekind", "requeststatus", "requestorigin")


def _column(table: str, name: str, dialect: str):
    """The column to add, built fresh per call because Alembic consumes it.

    ``request_id`` carries a foreign key everywhere the backend can express
    one in an ALTER. SQLite cannot: it has no ALTER for constraints, and
    the batch copy-and-move that works around it needs every reflected
    constraint to be named, which this schema does not do. So on SQLite the
    migrated column is a plain INTEGER.

    Behaviour is identical either way — SQLite does not enforce foreign
    keys unless ``PRAGMA foreign_keys=ON``, and the ORM reads its
    relationships from the model rather than the database — but the DDL
    genuinely differs from what ``create_all`` writes on a fresh SQLite
    file, so it is recorded here rather than discovered later. PostgreSQL,
    the supported target, gets the constraint.
    """
    import sqlalchemy as sa

    if name == "request_id":
        if dialect == "sqlite":
            return sa.Column(name, sa.Integer(), nullable=True)
        return sa.Column(name, sa.Integer(), sa.ForeignKey(f"{REQUESTS_TABLE}.id"), nullable=True)
    if name == "value_text":
        return sa.Column(name, sa.String(120), nullable=True)
    if name == "comparator":
        return sa.Column(name, sa.String(2), nullable=True)
    raise ValueError(f"{table}.{name}: no definition in this revision")


def upgrade() -> None:
    """Create service_requests, then add the columns that point at it."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(REQUESTS_TABLE):
        Base.metadata.tables[REQUESTS_TABLE].create(bind, checkfirst=True)
    for table, columns in NEW_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        missing = [name for name in columns if name not in existing]
        if not missing:
            continue
        for name in missing:
            op.add_column(table, _column(table, name, bind.dialect.name))


def downgrade() -> None:
    """Drop the columns first — they carry the FK into the table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    for table, columns in NEW_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name in columns:
            if name in existing:
                op.drop_column(table, name)
    if inspector.has_table(REQUESTS_TABLE):
        op.drop_table(REQUESTS_TABLE)
        if bind.dialect.name == "postgresql":
            for type_name in ENUM_TYPES:
                op.execute(f"DROP TYPE IF EXISTS {type_name}")
