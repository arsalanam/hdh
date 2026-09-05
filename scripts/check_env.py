#!/usr/bin/env python
"""Report whether the agent's API key is configured. Never prints the key."""

import os


def main() -> None:
    """Print whether ANTHROPIC_API_KEY is visible to project commands."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        print(f"ANTHROPIC_API_KEY is set (ends with ...{key[-4:]})")
    else:
        print("ANTHROPIC_API_KEY is NOT set.")
        print("  - project-local: copy .env.example to .env and fill it in")
        print("  - machine-wide (Windows): setx ANTHROPIC_API_KEY <your-key>")
        print("  - or authenticate once with: ant auth login")
    model = os.environ.get("HDH_AGENT_MODEL")
    if model:
        print(f"HDH_AGENT_MODEL override: {model}")

    umls = os.environ.get("UMLS_API_KEY") or os.environ.get("HDH_UMLS_API_KEY")
    if umls:
        print(f"UMLS_API_KEY is set (ends with ...{umls[-4:]}) — `hdh snomed load --download` available")
    else:
        print("UMLS_API_KEY not set — SNOMED download disabled (sign up at https://uts.nlm.nih.gov)")

    db_url = os.environ.get("HDH_DB_URL")
    if not db_url:
        print("HDH_DB_URL not set — using the SQLite file (transitional default).")
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    shown = make_url(db_url).render_as_string(hide_password=True)
    connect_args = {"connect_timeout": 3} if db_url.startswith("postgresql") else {}
    try:
        engine = create_engine(db_url, connect_args=connect_args)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"HDH_DB_URL: connected OK ({shown})")
        _report_revision(engine)
    except Exception:
        print(f"HDH_DB_URL is set ({shown}) but the database is not reachable.")
        print("  -> start the dependency containers with: just deps")

    # Compared REDACTED against REDACTED. The first version compared the
    # .env value (password hidden) against the live one (password intact),
    # so they never matched and the warning fired on every run — an alarm
    # that is always on is an alarm nobody reads.
    from_env = _dotenv_db_url()
    if from_env is not None and from_env != shown:
        # A shell variable WINS over .env: `just` loads the file but does not
        # override what is already in the environment. The symptom is a
        # column-does-not-exist traceback against a database nobody meant to
        # use, and nothing in the error says which database it was.
        print("  ! your shell sets HDH_DB_URL and it OVERRIDES .env")
        print(f"    .env would use: {from_env}")
        print("    -> unset it, or migrate this one: just db-upgrade")


def _dotenv_db_url() -> str | None:
    """What .env would have used, for comparison."""
    import pathlib

    env = pathlib.Path(".env")
    if not env.is_file():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("HDH_DB_URL="):
            from sqlalchemy.engine import make_url

            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            try:
                return make_url(raw).render_as_string(hide_password=True)
            except Exception:
                return raw
    return None


def _report_revision(engine) -> None:
    """Which migration this database is at, and whether that is head.

    "Connected OK" is not the same as "usable": a database can answer
    SELECT 1 and still be missing every column the code expects, which
    surfaces as a psycopg traceback dozens of frames deep that never names
    the database it was talking to.
    """
    from sqlalchemy import text as sql

    try:
        with engine.connect() as conn:
            current = conn.execute(sql("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        print("  ! no alembic_version table — this database has never been migrated")
        print("    -> just db-upgrade   (or just db-stamp, if it was built by create_all)")
        return

    head = _head_revision()
    if head and current != head:
        print(f"  ! schema is at {current}, head is {head} — MIGRATIONS PENDING")
        print("    -> just db-upgrade")
    else:
        print(f"  schema at {current} (head)")


def _head_revision() -> str | None:
    """The newest revision on disk, without importing alembic's config."""
    import pathlib
    import re

    revisions, parents = set(), set()
    for path in pathlib.Path("migrations/versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found = re.search(r"^revision = [\"'](\w+)[\"']", text, re.M)
        down = re.search(r"^down_revision = [\"'](\w+)[\"']", text, re.M)
        if found:
            revisions.add(found.group(1))
        if down:
            parents.add(down.group(1))
    tips = revisions - parents
    return sorted(tips)[-1] if tips else None


if __name__ == "__main__":
    main()
