"""an allergy is a record, not a substance string.

`substance` stays free text — it is what a clinician types and what an
imported record carries — and `drug_code` + `code_standard` carry the coded
form when one is known, rather than pretending the text was ever a code. The
standard travels with the code because a bare identifier cannot say whether
it is RxNorm or SNOMED, and guessing from its shape is how a drug allergy
becomes a food one.

`last_happened` is deliberately separate from `noted_date`. When someone
wrote it down and when it last actually happened are different questions,
and a severe reaction thirty years ago is weighed differently from one last
month.

Nothing here makes "never asked" recordable, and that is a decision rather
than an omission: in hdh a patient with no allergy rows has no known
allergies, because every chart is generated or written through the chart
tools and both record an allergy when there is one. The day charts arrive
from a source that may simply not have asked, this needs an explicit
"asked, and none" assertion — see docs/design/patient-chart-completeness.md.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

TABLE = "allergies"
COLUMNS = (
    ("drug_code", sa.String(32)),
    ("code_standard", sa.String(16)),
    ("last_happened", sa.Date()),
    ("notes", sa.Text()),
)


def _columns(inspector) -> set[str]:
    if TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _columns(inspect(op.get_bind()))
    if not existing:
        return
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = _columns(inspect(op.get_bind()))
    if not existing:
        return
    # batch_alter_table so SQLite recreates the table rather than refusing
    # the drop, matching migrations 0014, 0015 and 0018.
    with op.batch_alter_table(TABLE) as batch:
        for name, _type in COLUMNS:
            if name in existing:
                batch.drop_column(name)
