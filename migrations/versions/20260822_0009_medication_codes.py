"""rxnorm: code columns on the medication rows (#52 §7, rxnorm design §6).

A code ANNOTATES what was written and never replaces it, so these are
additive nullable columns beside the free text — the same shape
ServiceRequest already carries.

Guarded like 0006: ``create_all`` builds them on a fresh database but
never ALTERs an existing one, so a database that predates this revision
has the tables without the columns.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = ("prescriptions", "medication_statements")
COLUMNS = {"code_system": 20, "code": 40}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table in TABLES:
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, length in COLUMNS.items():
            if name not in existing:
                op.add_column(table, sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table in TABLES:
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name in COLUMNS:
            if name in existing:
                op.drop_column(table, name)
