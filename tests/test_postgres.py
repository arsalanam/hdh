"""PostgreSQL integration tests (opt-in via HDH_PG_TEST_URL).

Run them locally with `just deps` then `just test-pg`; CI runs them against
service containers. The URL must point at a scratch database the tests may
freely create and drop tables in (docker/pg-init.sql creates `hdh_test`).
"""

import os

import pytest
from sqlalchemy import create_engine, func, select, text

from hdh.core.generators import build_dataset
from hdh.core.migrate import MigrationError, migrate_sqlite
from hdh.core.models import Base, Patient, Sex, get_engine, get_session

PG_URL = os.environ.get("HDH_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="HDH_PG_TEST_URL not set (start containers with `just deps`)"
)


@pytest.fixture()
def pg_engine():
    """A clean PostgreSQL schema per test, dropped afterwards."""
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = create_engine(PG_URL, echo=False)
    Base.metadata.drop_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_generate_against_postgres(pg_engine):
    """The generator writes a coherent dataset straight into PostgreSQL."""
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        build_dataset(session, n_patients=12, years_of_history=1, verbose=False)
        assert session.query(func.count(Patient.id)).scalar() == 12
        from hdh.modules.caregaps.detector import detect_gaps

        detect_gaps(session)  # smoke: aggregate queries run on the pg dialect
    finally:
        session.close()
        engine.dispose()


def test_migrate_sqlite_to_postgres(pg_engine, tmp_path):
    """hdh migrate copies a SQLite dataset verbatim and fixes sequences."""
    sqlite_path = str(tmp_path / "src.db")
    src_engine = get_engine(sqlite_path)
    src_session = get_session(src_engine)
    build_dataset(src_session, n_patients=10, years_of_history=1, verbose=False)
    expected = {
        t.name: src_session.execute(select(func.count()).select_from(t)).scalar()
        for t in Base.metadata.sorted_tables
    }
    src_session.close()
    src_engine.dispose()

    results = migrate_sqlite(sqlite_path, pg_engine)
    assert results and all(r.verified for r in results)
    assert {r.table: r.rows for r in results} == {
        name: count for name, count in expected.items() if name in {r.table for r in results}
    }

    # sequences must be advanced: a fresh insert may not collide with copied ids
    session = get_session(pg_engine)
    try:
        from datetime import date

        session.add(
            Patient(
                mrn="MRN99999999",
                first_name="Seq",
                last_name="Check",
                date_of_birth=date(1990, 1, 1),
                sex=Sex.FEMALE,
            )
        )
        session.commit()
        assert session.query(Patient).filter_by(mrn="MRN99999999").one().id > 10
    finally:
        session.close()

    # a second run without --force must refuse, leaving the target intact
    with pytest.raises(MigrationError):
        migrate_sqlite(sqlite_path, pg_engine)

    # ...and with force=True it succeeds again
    results2 = migrate_sqlite(sqlite_path, pg_engine, force=True)
    assert all(r.verified for r in results2)


def test_check_env_connectivity_query(pg_engine):
    """The SELECT 1 probe check-env uses works on the pg dialect."""
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
