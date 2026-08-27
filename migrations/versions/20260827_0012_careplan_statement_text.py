"""careplan: plan statements are prose, not a 400-character field.

Every column a model writes prose into was bounded by a guessed width —
``statement`` at 400 characters, ``owner_role`` at 60, ``target_value``
at 60. Those held while the knowledge corpus was four chunks about one
drug class and the model had little to say. Widening the corpus to cover
fourteen chronic conditions immediately produced a 549-character
intervention — richer knowledge produces richer prose — and the insert
failed with ``StringDataRightTruncation`` partway through writing a plan.

The next run failed on ``owner_role``, with a model naming two roles and
their duties in 134 characters. Two in a row is a class rather than a
coincidence, so this widens all of them at once instead of discovering
the third in production.

None of these were clinical limits; they were defaults nobody had reason
to question. Codes, systems and hashes keep their widths — a SNOMED
identifier really does have a length — but the width of a model's
sentence is not a fact anyone knows in advance. The only alternative to
widening is truncating a clinical instruction at a character count, which
would have silently cut the second half of a deprescribing action.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-27
"""

from alembic import op
from sqlalchemy import Text, inspect

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: table -> the columns that carry model-authored prose.
#:
#: Codes, systems and hashes stay bounded — a SNOMED identifier really does
#: have a length. These are the columns a model writes into, and the width
#: of a model's sentence is not a fact anyone knows in advance.
COLUMNS = {
    "health_concerns": ("statement",),
    "plan_goals": ("statement", "target_value"),
    "plan_interventions": ("statement", "owner_role", "schedule"),
    "plan_outcomes": ("measure",),
}

#: Columns that may legitimately be empty, so the alter must not assert
#: NOT NULL on them.
NULLABLE = {"target_value", "owner_role", "schedule"}


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite has no ALTER COLUMN, and needs none: it does not enforce
    # declared VARCHAR lengths in the first place, so the bug being fixed
    # here never existed there. That is also why no offline test could have
    # caught it — the truncation is a PostgreSQL behaviour, and the careplan
    # module requires PostgreSQL anyway (ARCHITECTURE §4a).
    if bind.dialect.name != "postgresql":
        return

    inspector = inspect(bind)
    for table, columns in COLUMNS.items():
        # The careplan module may not be registered in this build, and a
        # fresh database gets Text from `create_all` without needing this.
        if not inspector.has_table(table):
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column not in present:
                continue
            op.alter_column(table, column, type_=Text(), existing_nullable=column in NULLABLE)


def downgrade() -> None:
    """Deliberately not narrowing back.

    Reversing this would truncate any statement written since the upgrade,
    which is data loss dressed as a schema change. The column stays wide;
    a narrower one was the bug.
    """
    return
