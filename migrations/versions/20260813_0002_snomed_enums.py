"""snomed: new enum values on the shared ontology tables.

SNOMED CT rows use ``kind='concept'`` (the semantic tag lives in
``properties.semantic_tag``) and ``edge_type='attribute'`` (one generic
edge type for all ~50 defining-attribute relationship types — design
snomed-module.md §3). PostgreSQL enum types must learn the new values;
SQLite stores enums as VARCHAR, so it needs nothing. The NEW tables
(ontology_terms, ontology_closure) come from the registry via
create_all/ensure_columns on fresh databases and Alembic autogenerate
picks them up on managed ones.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-13
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the SNOMED enum values (PostgreSQL only; irreversible by design)."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TYPE ontologyconcept_kind_enum ADD VALUE IF NOT EXISTS 'concept'")
    op.execute("ALTER TYPE ontologyedge_edge_type_enum ADD VALUE IF NOT EXISTS 'attribute'")


def downgrade() -> None:
    """PostgreSQL cannot drop enum values; the extra values are harmless."""
