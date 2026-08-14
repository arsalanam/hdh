"""The release guard (issue #31): no SNOMED CT content in release assets.

Uses the synthetic fixture as a stand-in for licensed content — the guard
cares about snomed_ct rows, not their provenance, so the license-clean
fixture exercises every code path."""

import sys
import zipfile
from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema

sys.path.insert(0, str(Path(__file__).parents[1]))  # repo root: scripts/ imports
from scripts.release_check import _url_for, check_database  # noqa: E402

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
ICD_FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"


@pytest.fixture()
def snomed_db(tmp_path):
    """A database holding both catalogs — a release candidate that must fail."""
    from hdh.modules.icd10cm.loader import run_load as icd_load
    from hdh.modules.snomed.loader import run_load as snomed_load

    bootstrap_schema()
    db = tmp_path / "candidate.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    snomed_load(session, SNOMED_FIXTURES)
    icd_load(session, ICD_FIXTURES, 2026)
    yield db, session
    session.close()
    engine.dispose()


def test_gate_blocks_snomed_content(snomed_db):
    db, _session = snomed_db
    violations = dict(check_database(f"sqlite:///{db.as_posix()}"))
    assert "snomed_ct concepts" in violations
    assert "snomed_ct closure rows" in violations
    assert "snomed_ct load-ledger rows" in violations


def test_gate_scans_inside_release_zip(snomed_db, tmp_path):
    db, _session = snomed_db
    asset = tmp_path / "family_medicine-release.zip"
    with zipfile.ZipFile(asset, "w") as zf:
        zf.write(db, "family_medicine.db")
    url = _url_for(str(asset), tmp_path / "scratch")
    assert check_database(url)  # violations found inside the zip


def test_purge_remediates_and_gate_passes(snomed_db):
    from hdh.modules.snomed.cli import _cmd_purge

    db, session = snomed_db
    with pytest.raises(SystemExit, match="--yes"):
        _cmd_purge(session, confirmed=False)  # destructive: refuses without --yes
    _cmd_purge(session, confirmed=True)
    assert check_database(f"sqlite:///{db.as_posix()}") == []
    # ICD-10-CM survives the purge — public domain, allowed in assets
    from sqlalchemy import func, select

    from hdh.core.models import Base

    concepts_t = Base.metadata.tables["ontology_concepts"]
    icd = session.execute(
        select(func.count()).select_from(concepts_t).where(concepts_t.c.ontology == "icd10cm")
    ).scalar()
    assert icd > 0


def test_gate_passes_pre_ontology_database(tmp_path):
    """A database without the ontology tables at all is clean by definition."""
    import sqlite3

    bare = tmp_path / "old.db"
    sqlite3.connect(bare).execute("CREATE TABLE patients (id INTEGER PRIMARY KEY)")
    assert check_database(f"sqlite:///{bare.as_posix()}") == []
