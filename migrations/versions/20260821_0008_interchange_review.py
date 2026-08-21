"""interchange: the review queue for results we refuse to file (#52 §6).

One new table, no column changes, so this is the simple half of the
guarded pattern 0005–0007 established: ``create_all`` builds it on a fresh
database, and this revision covers one that already exists.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-21
"""

from alembic import op
from sqlalchemy import inspect

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLE = "rejected_results"

#: PostgreSQL keeps a table's ENUM types after the table is dropped, so a
#: downgrade must remove them or the next upgrade collides (learned on 0005).
ENUM_TYPES = ("rejectedresult_reason_enum",)


def upgrade() -> None:
    from hdh.core.models import Base

    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        return
    table = Base.metadata.tables.get(TABLE)
    if table is None:  # the interchange module is not registered in this build
        return
    table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    op.drop_table(TABLE)
    if bind.dialect.name == "postgresql":
        for type_name in ENUM_TYPES:
            op.execute(f"DROP TYPE IF EXISTS {type_name}")
