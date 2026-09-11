"""where a provider practices — the link that gives a new encounter its site.

The location half of AU4 (issue #169): a provider works at one or more
locations, one flagged primary. `comprehension.apply_to_chart` reads the
recording provider's primary location and stamps it on the new Visit, so an
encounter can finally say where it happened. Additive and nullable — existing
rows are untouched.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

TABLE = "provider_locations"


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    # Needs providers and locations; both predate this revision (locations
    # arrived in 0025). Guard anyway so a partial database is a no-op.
    if not (_has_table(inspector, "providers") and _has_table(inspector, "locations")):
        return
    if _has_table(inspector, TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("provider_id", "location_id", name="uq_provider_location"),
    )
    op.create_index("ix_provider_locations_provider_id", TABLE, ["provider_id"])
    op.create_index("ix_provider_locations_location_id", TABLE, ["location_id"])


def downgrade() -> None:
    if _has_table(inspect(op.get_bind()), TABLE):
        op.drop_table(TABLE)
