"""Copy a SQLite hdh database into a PostgreSQL (or any SQLAlchemy) target.

The exit path from SQLite (docs/design/icd10cm-ontology-module.md §5.3):
``hdh migrate`` walks the registry-merged metadata in FK dependency order,
copies every table that exists in the source file, verifies row counts, and
advances PostgreSQL sequences so future inserts don't collide. The source
SQLite file is never modified — it remains its own backup.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, inspect, select, text

from hdh.core.models import Base


@dataclass(frozen=True)
class TableCopy:
    """Result of copying one table: name, rows copied, verified count match."""

    table: str
    rows: int
    verified: bool


class MigrationError(Exception):
    """A migration precondition or verification failure."""


def _assert_target_empty(target: Engine, tables) -> None:
    """Refuse to copy into a target that already holds rows (use --force)."""
    with target.connect() as conn:
        for table in tables:
            count = conn.execute(select(text("count(*)")).select_from(table)).scalar()
            if count:
                raise MigrationError(
                    f"target table '{table.name}' already has {count:,} rows — "
                    "re-run with --force to clear the target first"
                )


def _clear_target(target: Engine, tables) -> None:
    """Delete all rows in reverse FK order (children before parents)."""
    with target.begin() as conn:
        for table in reversed(tables):
            conn.execute(table.delete())


def _advance_sequences(target: Engine, tables) -> None:
    """Point each serial/identity sequence past the copied ids (PostgreSQL)."""
    if target.dialect.name != "postgresql":
        return
    with target.begin() as conn:
        for table in tables:
            if "id" not in table.columns or not table.columns["id"].autoincrement:
                continue
            # table.name comes from our own ORM metadata, never user input
            seq_sql = (
                "SELECT setval(pg_get_serial_sequence(:tab, 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"
            )
            conn.execute(text(seq_sql), {"tab": table.name})


def migrate_sqlite(
    source_path: str,
    target: Engine,
    batch_size: int = 5000,
    force: bool = False,
) -> list[TableCopy]:
    """Copy every metadata table present in ``source_path`` into ``target``.

    Copies the intersection of source and target columns (so a source file
    predating a schema extension still migrates), in FK dependency order,
    in batches. Returns one TableCopy per table with verification status.
    """
    # Short-lived read-only engine from this function's own source-path
    # argument; the target engine IS injected.
    source = create_engine(f"sqlite:///{source_path}", echo=False)  # quality: allow(dependency-injection)
    try:
        source_tables = set(inspect(source).get_table_names())
        if not source_tables:
            raise MigrationError(f"source '{source_path}' contains no tables")

        Base.metadata.create_all(target)
        tables = [t for t in Base.metadata.sorted_tables if t.name in source_tables]

        if force:
            _clear_target(target, tables)
        else:
            _assert_target_empty(target, tables)

        results: list[TableCopy] = []
        for table in tables:
            source_cols = {c["name"] for c in inspect(source).get_columns(table.name)}
            cols = [c for c in table.columns if c.name in source_cols]
            copied = 0
            with source.connect() as read_conn, target.begin() as write_conn:
                rows = read_conn.execution_options(yield_per=batch_size).execute(
                    select(*[table.c[c.name] for c in cols])
                )
                for batch in rows.partitions(batch_size):
                    if batch:
                        write_conn.execute(
                            table.insert(),
                            [dict(zip([c.name for c in cols], row, strict=True)) for row in batch],
                        )
                        copied += len(batch)
            with source.connect() as sconn, target.connect() as tconn:
                src_count = sconn.execute(select(text("count(*)")).select_from(table)).scalar()
                tgt_count = tconn.execute(select(text("count(*)")).select_from(table)).scalar()
            results.append(TableCopy(table.name, copied, src_count == tgt_count))

        _advance_sequences(target, tables)
        return results
    finally:
        source.dispose()
