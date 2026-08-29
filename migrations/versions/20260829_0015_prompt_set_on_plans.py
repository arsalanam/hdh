"""care plans record which prompt set produced them.

`docs/design/interactive-care-planning.md` §6. A prompt change leaves the
cohort, the charts and every MRN identical and moves only the scores, which
is exactly what a real improvement looks like. The stamp is what tells the
two apart afterwards.

Nullable by design: plans written before prompts were versioned cannot say
which wording produced them, and inventing a value for them would be worse
than an honest NULL.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-29
"""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TABLE = "care_plan_records"
COLUMN = "prompt_set"


def _has_column(inspector) -> bool:
    return TABLE in inspector.get_table_names() and COLUMN in {
        c["name"] for c in inspector.get_columns(TABLE)
    }


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if TABLE in inspector.get_table_names() and not _has_column(inspector):
        op.add_column(TABLE, Column(COLUMN, String(60)))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if _has_column(inspector):
        # batch_alter_table so SQLite recreates the table rather than
        # refusing the drop, matching migration 0014's approach.
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
