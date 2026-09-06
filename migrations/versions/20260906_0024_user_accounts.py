"""an authenticated identity links to a provider profile (AU2).

The one thing the chart needs from the identity system: which provider a
Keycloak subject writes as, so an audit event carries a `provider_id` and
not only a name. Credentials, roles and sessions stay in Keycloak and never
reach this database.

Keyed on `subject` (Keycloak's stable `sub`), not username — a username can
be reassigned, a subject cannot, and attribution has to survive a rename.
`provider_id` is nullable: an account is usable for attribution by name
before it is tied to a clinical profile.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

TABLE = "user_accounts"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "providers" not in inspector.get_table_names():
        return
    if TABLE in inspector.get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(64), nullable=False, unique=True),
        sa.Column("username", sa.String(120), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("providers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_user_accounts_subject", TABLE, ["subject"], unique=True)


def downgrade() -> None:
    if TABLE in inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
