"""core: appointments table + visits.appointment_id (provider scheduling).

The ``appointments`` table is a booked slot on a provider's calendar, and each
clinical ``Visit`` links to the appointment it fulfils via a new
``visits.appointment_id`` column.

New TABLES and columns are created by ``get_engine``'s create_all on any
database, so this revision is inspector-guarded — it exists to honor the rule
that every schema change ships its own revision (CONTRIBUTING, issue #30) and to
build the table/column on databases migrated exclusively through Alembic. The
column is added as a plain integer (the FK is enforced by the ORM and by
create_all), mirroring 0021's add-column pattern.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

TABLES = ("appointments",)


def _visit_columns(inspector) -> set[str]:
    if "visits" not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns("visits")}


def upgrade() -> None:
    """Create appointments and add visits.appointment_id if create_all has not."""
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in TABLES:
        if not inspector.has_table(name):
            Base.metadata.tables[name].create(bind)
    columns = _visit_columns(inspector)
    if columns and "appointment_id" not in columns:
        op.add_column("visits", sa.Column("appointment_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop visits.appointment_id, then the appointments table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if "appointment_id" in _visit_columns(inspector):
        with op.batch_alter_table("visits") as batch:
            batch.drop_column("appointment_id")
    from hdh.core.models import Base

    for name in reversed(TABLES):
        if inspector.has_table(name):
            Base.metadata.tables[name].drop(bind)
