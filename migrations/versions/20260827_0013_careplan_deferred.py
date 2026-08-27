"""careplan: record what a plan decided not to address.

Triage (#104) picks the topics a plan will cover and sets the rest aside.
Which ones it set aside is not a detail of the run that produced the plan —
it is part of the plan, and a reviewer reading it next month needs to see
that three problems were deliberately deferred rather than infer it from
their absence.

The rubric makes the same distinction. `multimorbid-elderly` scores
completeness at 4 for a plan that omits something "without saying why" and
at 5 when the omission is "defensible from the chart and visible to the
reader". Without somewhere to write it down, the second was unreachable.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-27
"""

from alembic import op
from sqlalchemy import JSON, Column, inspect

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TABLE = "care_plan_records"
COLUMN = "deferred"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    # The careplan module may not be registered in this build, and a fresh
    # database gets the column from `create_all` without needing this.
    if not inspector.has_table(TABLE):
        return
    if COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}:
        return
    op.add_column(TABLE, Column(COLUMN, JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(TABLE):
        return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        return
    op.drop_column(TABLE, COLUMN)
