"""identifiers, addresses, contacts and coverage become records (M3).

A person accumulates these. The chart held exactly one of each, in columns
on `patients`, so a second identifier, a former address, a mobile number
beside a landline, or a secondary insurer were all unrecordable — and the
answer to each was going to be another column.

The flat columns stay for now and are **backfilled into these tables**, so
the rows are correct from the moment they exist rather than empty until the
generator catches up. Until the readers move over, `patients.phone` and the
rank-1 contact are the same fact in two places, which is a real cost: a test
asserts they agree so the duplication cannot drift silently, and removing
the flat columns is its own migration once nothing reads them.

`kind`, `use` and `system` are free text with documented values rather than
enums. Extending a PostgreSQL enum needs a migration on every deployed
database — 0002 is the standing reminder — and an identifier type is exactly
the field the next deployment invents a value for.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TABLES = ("patient_identifiers", "patient_addresses", "patient_contacts", "patient_coverages")


def _period(*extra):
    return (
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        *extra,
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    if "patients" not in existing:
        return

    if "patient_identifiers" not in existing:
        op.create_table(
            "patient_identifiers",
            *_period(
                sa.Column("kind", sa.String(32), nullable=False),
                sa.Column("value", sa.String(64), nullable=False),
                sa.Column("issuer", sa.String(120), nullable=True),
            ),
        )
    if "patient_addresses" not in existing:
        op.create_table(
            "patient_addresses",
            *_period(
                sa.Column("use", sa.String(16), nullable=False, server_default="home"),
                sa.Column("line", sa.String(200), nullable=True),
                sa.Column("city", sa.String(80), nullable=True),
                sa.Column("state", sa.String(40), nullable=True),
                sa.Column("postal_code", sa.String(16), nullable=True),
                sa.Column("country", sa.String(60), nullable=True),
            ),
        )
    if "patient_contacts" not in existing:
        op.create_table(
            "patient_contacts",
            *_period(
                sa.Column("system", sa.String(16), nullable=False),
                sa.Column("use", sa.String(16), nullable=True),
                sa.Column("value", sa.String(120), nullable=False),
                sa.Column("rank", sa.Integer(), nullable=True),
            ),
        )
    if "patient_coverages" not in existing:
        op.create_table(
            "patient_coverages",
            *_period(
                sa.Column("rank", sa.Integer(), nullable=False, server_default="1"),
                sa.Column("payer_name", sa.String(120), nullable=False),
                sa.Column("member_id", sa.String(64), nullable=True),
                sa.Column("group_number", sa.String(64), nullable=True),
                sa.Column("policy_holder", sa.String(120), nullable=True),
            ),
        )
    _backfill(bind)


def _backfill(bind) -> None:
    """One row per flat value that is actually populated.

    Every statement carries its own NOT EXISTS on the discriminator it
    writes, so the backfill is idempotent per KIND rather than per table.
    The first version guarded on "is this table empty", and two statements
    write to `patient_contacts` — so inserting the phone rows made the table
    non-empty and the email insert was skipped. 164 email contacts went
    missing on the live database, and the table looked plausibly full at
    exactly one row per patient.

    Blank strings are skipped: an empty email is not a contact, and a row
    saying so is worse than no row because it reads as a recorded fact.
    """
    statements = (
        "INSERT INTO patient_identifiers (patient_id, kind, value) "
        "SELECT p.id, 'mrn', p.mrn FROM patients p "
        "WHERE p.mrn IS NOT NULL AND p.mrn <> '' AND NOT EXISTS ("
        "  SELECT 1 FROM patient_identifiers x WHERE x.patient_id = p.id AND x.kind = 'mrn')",
        "INSERT INTO patient_addresses (patient_id, use, line, city, state, postal_code) "
        "SELECT p.id, 'home', p.address, p.city, p.state, p.zip_code FROM patients p "
        "WHERE p.address IS NOT NULL AND p.address <> '' AND NOT EXISTS ("
        "  SELECT 1 FROM patient_addresses x WHERE x.patient_id = p.id AND x.use = 'home')",
        "INSERT INTO patient_contacts (patient_id, system, use, value, rank) "
        "SELECT p.id, 'phone', 'home', p.phone, 1 FROM patients p "
        "WHERE p.phone IS NOT NULL AND p.phone <> '' AND NOT EXISTS ("
        "  SELECT 1 FROM patient_contacts x WHERE x.patient_id = p.id AND x.system = 'phone')",
        "INSERT INTO patient_contacts (patient_id, system, use, value, rank) "
        "SELECT p.id, 'email', 'home', p.email, 2 FROM patients p "
        "WHERE p.email IS NOT NULL AND p.email <> '' AND NOT EXISTS ("
        "  SELECT 1 FROM patient_contacts x WHERE x.patient_id = p.id AND x.system = 'email')",
        "INSERT INTO patient_coverages (patient_id, rank, payer_name, member_id) "
        "SELECT p.id, 1, p.insurance_name, p.insurance_id FROM patients p "
        "WHERE p.insurance_name IS NOT NULL AND p.insurance_name <> '' AND NOT EXISTS ("
        "  SELECT 1 FROM patient_coverages x WHERE x.patient_id = p.id AND x.rank = 1)",
    )
    for statement in statements:
        bind.execute(text(statement))


def downgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    for table in reversed(TABLES):
        if table in existing:
            op.drop_table(table)
