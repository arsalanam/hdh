"""Prepare a PostgreSQL database so every module can actually run.

`create_all` builds the tables. It does not install extensions, and two
features need them — so a database created the documented way came up
missing exactly the parts that are hardest to diagnose later:

- **`pg_trgm`** — trigram retrieval raises without it. Full-text search
  still answers, so the failure is intermittent: it appears only on the
  queries FTS misses, which are the ones the fallback exists for.
- **`vector`** and `knowledge_chunks.embedding` — semantic retrieval cannot
  run at all, and the refusal points at a database nobody told you to
  prepare. Both belong to the care-plan module, which contributes them
  through ``extra`` rather than being named here: core does not know that
  module exists.

The migrations do install them (0011 and 0017). But migrations are for
databases that already exist: `alembic upgrade head` from zero fails at
0002, which alters an enum that `create_all` creates and no migration does.
So the fresh path is create_all → stamp → *this* → and the extension work
had nowhere to live until now.

Idempotent, and safe to run against a database that is already correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Extensions **core** needs, and what stops working without each one.
#:
#: `pg_trgm` only. `vector` is the care-plan module's, and core does not
#: know that module exists — the dependency rule the architecture is built
#: on, and one this file broke on its first draft by importing the module
#: for a constant. Modules contribute their own through :func:`initialise`.
EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("pg_trgm", "trigram retrieval — the fallback when full-text search misses"),
)


@dataclass
class InitReport:
    """What was already right, what was fixed, and what could not be."""

    installed: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    embedding_column: str = ""

    @property
    def ok(self) -> bool:
        """Nothing missing that this database could have had."""
        return not self.unavailable

    def lines(self) -> list[str]:
        out = []
        for name in self.already:
            out.append(f"  ok         {name}")
        for name in self.installed:
            out.append(f"  installed  {name}")
        for name, why in self.unavailable:
            out.append(f"  MISSING    {name} — not available on this server ({why})")
        if self.embedding_column:
            out.append(f"  {self.embedding_column}")
        return out


def initialise(session, extra: tuple[tuple[str, str], ...] = ()) -> InitReport:
    """Install what this database needs, and report what could not be.

    ``extra`` is how a module adds an extension it owns without core
    learning that the module exists. The CLI composes the list; core
    supplies only its own.

    Never raises for a missing extension. A server that does not ship
    pgvector is a real deployment, and the modules that need it already
    refuse clearly — an install command that dies is worse than one that
    says which feature will be unavailable and why.
    """
    from sqlalchemy import text as sql_text

    from hdh.core.dialect import require_postgresql

    require_postgresql(session, "Database initialisation")
    report = InitReport()

    for name, purpose in (*EXTENSIONS, *extra):
        installed = session.execute(
            sql_text("SELECT count(*) FROM pg_extension WHERE extname = :n"), {"n": name}
        ).scalar()
        if installed:
            report.already.append(name)
            continue
        available = session.execute(
            sql_text("SELECT count(*) FROM pg_available_extensions WHERE name = :n"), {"n": name}
        ).scalar()
        if not available:
            report.unavailable.append((name, purpose))
            continue
        session.execute(sql_text(f"CREATE EXTENSION IF NOT EXISTS {name}"))
        session.commit()
        report.installed.append(name)

    return report
