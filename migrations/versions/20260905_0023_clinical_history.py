"""procedures get codes, vaccines can be refused, allergies get status (M5).

Three changes, one theme: the chart could describe these things and could
not say the part another clinician needs.

**Procedures** carried free text and nothing codeable. "Repair" and
"revision" read alike and bill differently, and `laterality` is the field
that makes a wrong-side procedure detectable — buried in a description it is
unqueryable.

**Immunisations** could only record a dose that was given. A declined
vaccine was unrecordable, so its absence looked identical to never having
been offered — and those are different clinical pictures. A care gap that
cannot tell them apart proposes the same thing every year.

`administered_date` therefore stops being NOT NULL. A refusal has no
administration date, and inventing one to satisfy a constraint records a
dose that was never given. Existing rows are stamped `status='completed'`,
so nothing written before this changes meaning.

**Allergies** gain category, criticality, verification and clinical status.
The distinction worth the column is criticality against severity: severity
is how bad the reaction that happened was, criticality is the risk that the
next one kills them. A mild rash to penicillin can still be high
criticality, and the chart previously had no way to say so.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

ADDED = {
    "procedures": (
        ("code", sa.String(32)),
        ("code_standard", sa.String(16)),
        ("body_site", sa.String(80)),
        ("laterality", sa.String(16)),
    ),
    "immunizations": (
        ("status", sa.String(16)),
        ("reason", sa.Text()),
        ("recorded_date", sa.Date()),
    ),
    "allergies": (
        ("category", sa.String(16)),
        ("criticality", sa.String(16)),
        ("verification", sa.String(16)),
        ("clinical_status", sa.String(16)),
    ),
}


def _columns(inspector, table) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table, columns in ADDED.items():
        existing = _columns(inspector, table)
        if not existing:
            continue
        for name, type_ in columns:
            if name not in existing:
                op.add_column(table, sa.Column(name, type_, nullable=True))

    if _columns(inspect(bind), "immunizations"):
        # Every row that exists was administered — that is all the table
        # could hold. Stamping them keeps their meaning once `status` can
        # say otherwise; leaving them NULL would make every historical dose
        # indistinguishable from one whose status nobody set.
        bind.execute(
            text("UPDATE immunizations SET status = 'completed' WHERE status IS NULL")
        )
        # A refusal has no administration date.
        with op.batch_alter_table("immunizations") as batch:
            batch.alter_column("administered_date", existing_type=sa.Date(), nullable=True)

    if _columns(inspect(bind), "allergies"):
        bind.execute(
            text("UPDATE allergies SET clinical_status = 'active' WHERE clinical_status IS NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _columns(inspector, "immunizations"):
        bind.execute(text("DELETE FROM immunizations WHERE administered_date IS NULL"))
        with op.batch_alter_table("immunizations") as batch:
            batch.alter_column("administered_date", existing_type=sa.Date(), nullable=False)
    for table, columns in ADDED.items():
        existing = _columns(inspect(bind), table)
        if not existing:
            continue
        with op.batch_alter_table(table) as batch:
            for name, _type in columns:
                if name in existing:
                    batch.drop_column(name)
