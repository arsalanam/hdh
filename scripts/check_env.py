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
    except Exception:
        print(f"HDH_DB_URL is set ({shown}) but the database is not reachable.")
        print("  → start the dependency containers with: just deps")


if __name__ == "__main__":
    main()
