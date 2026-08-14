#!/usr/bin/env python
"""Release guard: FAIL if a candidate database contains SNOMED CT content.

SNOMED CT is licensed and must never be redistributed (design
snomed-module.md §2; issue #31). Release assets ship whole databases, so
this check gates every asset build: ICD-10-CM in an asset is fine (CMS
files are public domain); one SNOMED row is a licensing violation.

Usage:
    python scripts/release_check.py <target>

<target> is any of:
    a SQLite file            family_medicine.db
    a zip containing one     family_medicine-10k.zip
    a SQLAlchemy URL         postgresql+psycopg://.../hdh

Run it against the SOURCE database BEFORE `pg_dump` / zipping — a
compressed dump can't be scanned reliably, the live database can.
Exit 0 = clean; exit 1 = licensed content found (remediate with
`hdh snomed purge`); exit 2 = target unusable.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

# (description, SQL returning a count) — every query must return 0.
# Missing tables count as clean: a pre-ontology database has nothing to leak.
CHECKS = (
    ("snomed_ct concepts", "SELECT COUNT(*) FROM ontology_concepts WHERE ontology = 'snomed_ct'"),
    ("snomed_ct term rows", "SELECT COUNT(*) FROM ontology_terms WHERE concept_id LIKE 'snomed_ct:%'"),
    (
        "snomed_ct edges",
        "SELECT COUNT(*) FROM ontology_edges "
        "WHERE source_id LIKE 'snomed_ct:%' OR target_id LIKE 'snomed_ct:%'",
    ),
    ("snomed_ct closure rows", "SELECT COUNT(*) FROM ontology_closure WHERE ancestor_id LIKE 'snomed_ct:%'"),
    ("snomed_ct load-ledger rows", "SELECT COUNT(*) FROM ontology_loads WHERE ontology = 'snomed_ct'"),
)


def _url_for(target: str, scratch: Path) -> str:
    """Resolve the target to a SQLAlchemy URL (extracting a zipped db)."""
    if "://" in target:
        return target
    path = Path(target)
    if not path.exists():
        raise SystemExit(f"release-check: no such file: {target}")
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".db")]
            if len(members) != 1:
                raise SystemExit(f"release-check: expected exactly one .db in {path.name}, found {members}")
            extracted = Path(zf.extract(members[0], scratch))
        return f"sqlite:///{extracted.as_posix()}"
    return f"sqlite:///{path.as_posix()}"


def check_database(url: str) -> list[tuple[str, int]]:
    """Run every guard query; return the (description, count) violations."""
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    violations = []
    try:
        known = set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            for description, sql in CHECKS:
                table = sql.split(" FROM ")[1].split(" WHERE ")[0].strip()
                if table not in known:
                    continue  # nothing to leak
                count = conn.execute(text(sql)).scalar() or 0
                if count:
                    violations.append((description, count))
    finally:
        engine.dispose()
    return violations


def main() -> int:
    """Gate one target; print the verdict."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    with tempfile.TemporaryDirectory() as scratch:
        try:
            url = _url_for(target, Path(scratch))
            violations = check_database(url)
        except SystemExit as err:
            print(err)
            return 2
    if violations:
        print(f"RELEASE BLOCKED — licensed SNOMED CT content in {target}:")
        for description, count in violations:
            print(f"   {count:>12,}  {description}")
        print(
            "\nSNOMED CT must never be redistributed (design snomed-module.md §2).\n"
            "Strip it from a copy of the database with:  hdh snomed purge --yes\n"
            "then re-run this check before building any release asset."
        )
        return 1
    print(f"release-check: {target} is clean — no SNOMED CT content (ICD-10-CM is public domain and fine).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
