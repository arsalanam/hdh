"""careplan: the plan graph — concerns, goals, interventions, outcomes.

Six new tables, no column changes, so this follows the simple half of the
guarded pattern 0005–0008 established: ``create_all`` builds them on a
fresh database, and this revision covers one that already exists.

Creation order matters here in a way it did not for 0008: the tables carry
foreign keys to each other, so a plan has to exist before a concern can
point at it. ``sorted_tables`` would do it, but naming the order makes the
graph visible in the migration too.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-25
"""

from alembic import op
from sqlalchemy import inspect

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

#: Dependency order: a row cannot reference a table that does not exist yet.
TABLES = (
    "care_plan_records",
    "health_concerns",
    "plan_goals",
    "plan_interventions",
    "plan_outcomes",
    "plan_evaluations",
)

#: PostgreSQL keeps a table's ENUM types after the table is dropped, so a
#: downgrade must remove them or the next upgrade collides (learned on 0005).
ENUM_TYPES = (
    "careplanrecord_status_enum",
    "healthconcern_concern_type_enum",
    "healthconcern_source_enum",
    "plangoal_expressed_by_enum",
    "plangoal_status_enum",
    "plangoal_source_enum",
    "planintervention_intervention_type_enum",
    "planintervention_source_enum",
    "planoutcome_achievement_status_enum",
    "planevaluation_verdict_enum",
)


def upgrade() -> None:
    from hdh.core.models import Base

    bind = op.get_bind()
    inspector = inspect(bind)
    for name in TABLES:
        if inspector.has_table(name):
            continue
        table = Base.metadata.tables.get(name)
        if table is None:  # the careplan module is not registered in this build
            continue
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for name in reversed(TABLES):  # children before parents
        if inspector.has_table(name):
            op.drop_table(name)
    if bind.dialect.name == "postgresql":
        for type_name in ENUM_TYPES:
            op.execute(f"DROP TYPE IF EXISTS {type_name}")
