"""a care plan can record the plan it replaces.

An approved plan is a record of a decision, so amending one must not edit
it. The revision becomes a new row carrying `supersedes_id`, and the
approved plan stays exactly as it was signed off.

What this column deliberately does not do is change the superseded plan's
status. That plan *was* approved; superseding it does not un-approve it,
and rewriting the field would destroy the fact the whole arrangement exists
to keep. Whether a plan is still in force is derived from the absence of a
successor, not stored — which also means no new enum value, and migration
0002 is the standing reminder of why that matters on a deployed PostgreSQL.

Nullable, indexed, and deliberately **not** a foreign key even though it
points at this table's own id. It would be the registry's only
self-referential FK, and that is a cycle for the SQLite->PostgreSQL copier,
which breaks cycles by looking for `use_alter` — something the registry has
no way to express. The value is only ever written from a row just read, and
a dangling one degrades to "no successor", which is what NULL already means.
The index earns its place because the question asked of this column is
always the reverse one: *what replaced this plan*.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

TABLE = "care_plan_records"
COLUMN = "supersedes_id"
INDEX = "ix_care_plan_supersedes"


def _columns(inspector) -> set[str]:
    if TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = _columns(inspector)
    if not existing:
        # The care-plan tables are created by the registry on a database
        # that has never had them; nothing to alter.
        return
    if COLUMN in existing:
        return

    op.add_column(TABLE, sa.Column(COLUMN, sa.Integer(), nullable=True))
    op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    if COLUMN not in _columns(inspect(op.get_bind())):
        return
    op.drop_index(INDEX, table_name=TABLE)
    # batch_alter_table so SQLite recreates the table rather than refusing
    # the drop, matching migrations 0014 and 0015 on this same table.
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
