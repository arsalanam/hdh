"""Alembic environment: migrations against the registry-merged metadata.

The critical line is ``bootstrap_schema()`` *before* ``target_metadata`` is
read (original design §13): the schema registry must run in the Alembic
process so autogenerate sees every module's extension columns and entities,
not just the static models. Editing a module's ``schema/entities/*.json``
and running ``just db-revision "msg"`` therefore produces the right diff.
"""

import os

from sqlalchemy import create_engine

from alembic import context
from hdh.core.schema_registry import bootstrap_schema

bootstrap_schema()  # must precede the metadata import — see module docstring

from hdh.core.models import Base  # noqa: E402  (needs the bootstrap above)

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """ini override (tests) → HDH_DB_URL (`just deps`) → local SQLite file."""
    ini_url = config.get_main_option("sqlalchemy.url", "")
    return ini_url or os.environ.get("HDH_DB_URL") or "sqlite:///family_medicine.db"


def _render_item(type_, obj, autogen_context):
    """Render JSONB variants importably. Alembic's default repr for JSONB
    emits ``astext_type=Text()`` with ``Text`` never imported — a NameError
    in the generated migration. Our registry only ever uses plain JSONB."""
    from sqlalchemy.dialects.postgresql import JSONB

    if type_ == "type" and isinstance(obj, JSONB):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "postgresql.JSONB()"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (alembic --sql)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite ALTERs need batch mode during the transition
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine."""
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
