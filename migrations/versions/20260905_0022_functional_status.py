"""what a person can actually do (M4).

The care-plan rubric grades `feasibility_burden` — "could this patient
actually carry out this plan?" — from an intervention count against a flat
limit of eight. Four interventions for someone who cannot leave the house
scored the same as four for someone who drives, because nothing in the chart
said which this was.

One row per domain, holding the current state. A reassessment updates the
row; how it changed belongs to `chart_audit_events` with every other
correction.

No backfill, and that is the point. Absence here means **unassessed**, which
is not a synonym for normal — the opposite of the allergy contract, and
deliberately so. An allergy nobody recorded is an allergy nobody has,
because the chart tools always ask. Nothing asks about function, so silence
is ignorance, and inventing `independent` rows for two hundred patients
would manufacture exactly the reassurance this milestone exists to stop.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLE = "functional_status"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "patients" not in inspector.get_table_names():
        return
    if TABLE in inspector.get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("domain", sa.String(24), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("aid", sa.String(80), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("code", sa.String(32), nullable=True),
        sa.Column("code_standard", sa.String(16), nullable=True),
        sa.Column("assessed_date", sa.Date(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_functional_status_domain", TABLE, ["patient_id", "domain"])


def downgrade() -> None:
    if TABLE in inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
