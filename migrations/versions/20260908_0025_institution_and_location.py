"""institutions, locations, their specialties/hours, and shared address/contact.

The institution half of the thin-entity plan (core-chart-expansion §3): the
organisation a service is billed to, the location it happened at, the
specialty clinics a location runs (each with its own phone and weekly hours),
and the shared `addresses`/`contacts` tables that serve both — one table each,
owned through a real FK with a CHECK that exactly one owner is set.

Visits and procedures gain a nullable `location_id` so the chart can finally
say *where* a vital, an encounter, or a procedure was recorded. Nullable, so
no existing row is invalidated and no re-baseline is forced; the generator
seeds a practice organisation and its locations but does not yet stamp every
visit (that, and the cohort re-baseline it needs, is a tracked follow-up).

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

NEW_TABLES = ("organizations", "locations", "location_specialties", "service_hours", "addresses", "contacts")


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    return _has_table(inspector, table) and column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = inspect(op.get_bind())

    if not _has_table(inspector, "organizations"):
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("tax_id", sa.String(32), nullable=True),
            sa.Column("npi", sa.String(20), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=True),
        )

    if not _has_table(inspector, "locations"):
        op.create_table(
            "locations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=True),
        )
        op.create_index("ix_locations_organization_id", "locations", ["organization_id"])

    if not _has_table(inspector, "location_specialties"):
        op.create_table(
            "location_specialties",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=False),
            sa.Column("specialty_id", sa.Integer(), sa.ForeignKey("specialties.id"), nullable=False),
            sa.Column("phone", sa.String(40), nullable=True),
            sa.UniqueConstraint("location_id", "specialty_id", name="uq_location_specialty"),
        )
        op.create_index("ix_location_specialties_location_id", "location_specialties", ["location_id"])
        op.create_index("ix_location_specialties_specialty_id", "location_specialties", ["specialty_id"])

    if not _has_table(inspector, "service_hours"):
        op.create_table(
            "service_hours",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "location_specialty_id",
                sa.Integer(),
                sa.ForeignKey("location_specialties.id"),
                nullable=False,
            ),
            sa.Column("day_of_week", sa.Integer(), nullable=False),
            sa.Column("opens", sa.Time(), nullable=True),
            sa.Column("closes", sa.Time(), nullable=True),
        )
        op.create_index("ix_service_hours_ls_id", "service_hours", ["location_specialty_id"])

    _owner_check = (
        "(CASE WHEN organization_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN location_id IS NULL THEN 0 ELSE 1 END) = 1"
    )

    if not _has_table(inspector, "addresses"):
        op.create_table(
            "addresses",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=True),
            sa.Column("use", sa.String(16), nullable=False),
            sa.Column("line", sa.String(200), nullable=True),
            sa.Column("city", sa.String(80), nullable=True),
            sa.Column("state", sa.String(40), nullable=True),
            sa.Column("postal_code", sa.String(16), nullable=True),
            sa.Column("country", sa.String(60), nullable=True),
            sa.Column("period_start", sa.Date(), nullable=True),
            sa.Column("period_end", sa.Date(), nullable=True),
            sa.CheckConstraint(_owner_check, name="ck_address_one_owner"),
        )
        op.create_index("ix_addresses_organization_id", "addresses", ["organization_id"])
        op.create_index("ix_addresses_location_id", "addresses", ["location_id"])

    if not _has_table(inspector, "contacts"):
        op.create_table(
            "contacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=True),
            sa.Column("system", sa.String(16), nullable=False),
            sa.Column("use", sa.String(16), nullable=True),
            sa.Column("value", sa.String(120), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=True),
            sa.Column("period_start", sa.Date(), nullable=True),
            sa.Column("period_end", sa.Date(), nullable=True),
            sa.CheckConstraint(_owner_check, name="ck_contact_one_owner"),
        )
        op.create_index("ix_contacts_organization_id", "contacts", ["organization_id"])
        op.create_index("ix_contacts_location_id", "contacts", ["location_id"])

    if _has_table(inspector, "visits") and not _has_column(inspector, "visits", "location_id"):
        op.add_column(
            "visits", sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=True)
        )
    if _has_table(inspector, "procedures") and not _has_column(inspector, "procedures", "location_id"):
        op.add_column(
            "procedures", sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=True)
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    # Drop the FK columns FIRST, while `locations` still exists, and via
    # batch_alter_table so SQLite rebuilds the table without the column (a
    # plain drop_column is unsupported there). Leaving the column while
    # dropping `locations` would strand a foreign key that a later
    # migration's own batch_alter_table cannot reflect past.
    if _has_column(inspector, "procedures", "location_id"):
        with op.batch_alter_table("procedures") as batch:
            batch.drop_column("location_id")
    if _has_column(inspector, "visits", "location_id"):
        with op.batch_alter_table("visits") as batch:
            batch.drop_column("location_id")
    for table in (
        "contacts",
        "addresses",
        "service_hours",
        "location_specialties",
        "locations",
        "organizations",
    ):
        if _has_table(inspector, table):
            op.drop_table(table)
