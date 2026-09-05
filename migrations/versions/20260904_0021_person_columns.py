"""a preferred name, a gender identity, pronouns, and a registered GP (M3).

Separate from 0020 because it is a different kind of change — columns on an
existing row rather than new tables — and because each is independently
useful and independently reversible.

`gender_identity` is recorded beside `sex` rather than replacing it. `sex`
is administrative and drives things that legitimately need it; identity is
what the person is, and the chart has to be able to say so when the two
differ. Inferring one from the other is how a system misgenders someone in
every letter it sends, and `pronouns` is here for the same reason — a name
does not carry them and neither does an administrative sex.

`preferred_name` is what they answer to. Empty is the ordinary case.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

TABLE = "patients"
COLUMNS = (
    ("preferred_name", sa.String(80)),
    ("gender_identity", sa.String(60)),
    ("pronouns", sa.String(40)),
    ("registered_provider_id", sa.Integer()),
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
    with op.batch_alter_table(TABLE) as batch:
        for name, _type in COLUMNS:
            if name in existing:
                batch.drop_column(name)
