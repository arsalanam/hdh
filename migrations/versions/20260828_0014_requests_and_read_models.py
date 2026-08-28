"""requests and read models: drug identity, supply, and fulfilment links.

`docs/design/requests-and-read-models.md`. A request is an intent; a read
model is written only as the outcome of a fulfilment. Every request-shaped
column in the schema was empty — `lab_results.request_id` 0 of 8,309,
`prescriptions.request_id` 0 of 2,175, `service_requests.end_date` 0 of
1,705, 0 requests COMPLETED — because the generator wrote chart rows
directly and the request layer was never on the path.

Three things, all additive:

- **`medications`** — drug identity once, instead of a string repeated on
  3,728 rows across two tables. Five of 56 drugs already disagreed with
  themselves, three of them carrying a blank class, which defeats the
  duplicate-class guard outright.
- **`medication_dispenses`** — the supply event. Patient-anchored, because
  a refill does not happen at a visit and `Prescription` (no `patient_id`,
  reachable only through a visit) is the wrong shape to hold one.
- **`request_id`** on `procedures` and `visits`, nullable. NULL means "not
  ordered" — historical rows, external imports, and things nobody asked
  for, all of which a chart has to be able to say.

`visits.request_id` closes a foreign-key cycle (condition → visit →
request → condition), so it is added with `use_alter`: the constraint lands
after the tables exist rather than forcing an unorderable create.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-28
"""

from alembic import op
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    inspect,
)

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _has(inspector, table: str, column: str) -> bool:
    return table in inspector.get_table_names() and column in {
        c["name"] for c in inspector.get_columns(table)
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "medications" not in tables:
        op.create_table(
            "medications",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("name", String(100), nullable=False, unique=True),
            Column("drug_class", String(80)),
            Column("class_qualifier", String(80)),
            Column("form", String(40)),
            Column("code_system", String(20)),
            Column("code", String(40)),
        )

    if "medication_dispenses" not in tables:
        op.create_table(
            "medication_dispenses",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("patient_id", Integer, ForeignKey("patients.id"), nullable=False),
            Column("request_id", Integer, ForeignKey("service_requests.id")),
            Column("medication_id", Integer, ForeignKey("medications.id")),
            Column("drug_name", String(100), nullable=False),
            Column("dispensed_date", Date, nullable=False),
            Column("quantity", Float),
            Column("days_supply", Integer),
            Column("origin", String(13), nullable=False, server_default="GENERATED"),
            Column("visit_id", Integer, ForeignKey("visits.id")),
            Column("voided_at", DateTime),
        )
        op.create_index("ix_dispense_patient", "medication_dispenses", ["patient_id"])
        op.create_index("ix_dispense_request", "medication_dispenses", ["request_id"])

    if "procedures" in tables and not _has(inspector, "procedures", "request_id"):
        # Same reasoning as `visits` below: the constraint is PostgreSQL's,
        # the column is everyone's.
        op.add_column("procedures", Column("request_id", Integer))
        if bind.dialect.name == "postgresql":
            op.create_foreign_key(
                "fk_procedures_request_id", "procedures", "service_requests", ["request_id"], ["id"]
            )

    if "visits" in tables and not _has(inspector, "visits", "request_id"):
        # Named and deferred: this edge closes the condition → visit →
        # request → condition cycle, so the constraint is added separately
        # from the column.
        op.add_column("visits", Column("request_id", Integer))
        if bind.dialect.name == "postgresql":
            # PostgreSQL only. SQLite does not enforce foreign keys by
            # default, and it cannot drop a constraint independently of the
            # column — so an inline one here makes the downgrade impossible
            # rather than merely unenforced.
            op.create_foreign_key(
                "fk_visits_request_id", "visits", "service_requests", ["request_id"], ["id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # `batch_alter_table` on both: SQLite cannot drop a column that a
    # foreign key still names, and it cannot drop the constraint separately
    # either — so the table is recreated without them. On PostgreSQL the
    # same call issues a plain ALTER.
    for table, constraint in (
        ("visits", "fk_visits_request_id"),
        ("procedures", "fk_procedures_request_id"),
    ):
        if not _has(inspector, table, "request_id"):
            continue
        with op.batch_alter_table(table) as batch:
            if bind.dialect.name == "postgresql":
                batch.drop_constraint(constraint, type_="foreignkey")
            batch.drop_column("request_id")
    if "medication_dispenses" in tables:
        op.drop_table("medication_dispenses")
    if "medications" in tables:
        op.drop_table("medications")
