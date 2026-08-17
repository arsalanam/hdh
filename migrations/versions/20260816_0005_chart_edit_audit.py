"""chartedit: audit trail + voided_at on the amendable entities (#40).

The new TABLE is created by ``get_engine``'s create_all on any database,
so that part is an inspector-guarded no-op there. The new COLUMNS are
not: ``create_all`` never alters an existing table, so a database built
before this revision has chart tables without ``voided_at`` — exactly the
stale-schema class of failure issue #30 was about. Both halves are
guarded, so this revision is safe to run on a fresh database, a stamped
one, or one that predates chart editing entirely.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16
"""

from alembic import op
from sqlalchemy import DateTime, inspect

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

AUDIT_TABLE = "chart_audit_events"
VOIDABLE_TABLES = ("conditions", "visits", "vitals", "prescriptions", "lab_results", "allergies")


def upgrade() -> None:
    """Create the audit table and add voided_at wherever it is missing."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(AUDIT_TABLE):
        # checkfirst: PostgreSQL keeps ENUM *types* when a table is
        # dropped, so a re-upgrade after a downgrade would otherwise fail
        # with DuplicateObject on CREATE TYPE.
        Base.metadata.tables[AUDIT_TABLE].create(bind, checkfirst=True)
    for table in VOIDABLE_TABLES:
        if not inspector.has_table(table):
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "voided_at" not in columns:
            op.add_column(table, sa_column())


def sa_column():
    """The column, built fresh per call (Alembic consumes the object)."""
    import sqlalchemy as sa

    return sa.Column("voided_at", DateTime(), nullable=True)


def downgrade() -> None:
    """Drop voided_at and the audit table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    for table in VOIDABLE_TABLES:
        if not inspector.has_table(table):
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "voided_at" in columns:
            op.drop_column(table, "voided_at")
    if inspector.has_table(AUDIT_TABLE):
        op.drop_table(AUDIT_TABLE)
        if bind.dialect.name == "postgresql":
            # the table's ENUM types outlive it — drop them too, or the
            # next upgrade collides with them
            for type_name in ("editsource", "auditaction"):
                op.execute(f"DROP TYPE IF EXISTS {type_name}")
