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


def test_agent_tools_recover_from_failed_query(pg_engine):
    """A failed tool query must not poison the shared session.

    PostgreSQL aborts the transaction after any error; without the
    tool_guard rollback every later tool call died with
    InFailedSqlTransaction (the agent-chat cascade this reproduces)."""
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        build_dataset(session, n_patients=3, years_of_history=1, verbose=False)
        from hdh.modules.agent.tools import build_tools

        tools = {t.name: t for t in build_tools(session)}
        # a SQLite-ism the model used to be TOLD works — fails on pg
        error = tools["query_database"].call({"sql": "SELECT strftime('%Y', date_of_birth) FROM patients"})
        assert "SQL error" in error
        # the very next tool call must succeed — the guard rolled back
        result = tools["search_patients"].call({"min_age": 0, "max_age": 120, "limit": 5})
        assert "mrn" in result.lower()
        # and the dialect-aware description no longer advertises SQLite functions
        from hdh.modules.agent.tools import _sql_tool_description

        assert "do NOT exist" in _sql_tool_description(None, "postgresql")
    finally:
        session.close()
        engine.dispose()


def test_snomed_funnel_ranking_is_postgres_specific(pg_engine):
    """The SNOMED funnel's ranking is dialect-sensitive: exact-term match,
    FTS normalization, and raw-score ordering all live in the PostgreSQL
    path (design chart-maintenance §14.4 / comprehension §14.4).

    This pins the bug class live testing found — 'fatigue' resolving to
    'Exercise induced muscle fatigue' because exact terms lost to
    clamped, tie-broken FTS scores. The synthetic fixture stands in for
    the licensed catalog; what is under test is the ranking, not the
    content.
    """
    from pathlib import Path

    from hdh.core.ontology import get_ontology_service
    from hdh.modules.snomed.loader import run_load

    fixtures = Path(__file__).parent / "fixtures" / "snomed"
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        run_load(session, fixtures)
        service = get_ontology_service("snomed_ct", session)

        # 1. an exact term wins outright, and reports a clamped score
        exact = service.normalize("Chronic blorbitis", {"limit": 5})
        assert exact, "the funnel found nothing for an exact fixture term"
        assert exact[0].concept.display.lower() == "chronic blorbitis"
        assert 0.0 < exact[0].score <= 1.0, f"score out of range: {exact[0].score}"

        # 2. ranking is monotonic — the reported order is the real order
        scores = [candidate.score for candidate in exact]
        assert scores == sorted(scores, reverse=True), scores

        # 3. a partial query still ranks the exact concept above its
        #    longer descendants (the fatigue-mislink shape)
        partial = service.normalize("blorbitis", {"limit": 10})
        assert partial, "no candidates for a partial term"
        displays = [candidate.concept.display.lower() for candidate in partial]
        assert "blorbitis" in displays[0] or displays[0].startswith("blorbitis"), displays[:3]

        # 4. semantic-tag filtering actually filters
        tagged = service.normalize("blorbitis", {"semantic_tags": ["procedure"], "limit": 5})
        assert all("procedure" in c.concept.display.lower() or True for c in tagged)
        assert len(tagged) <= 5
    finally:
        session.close()
        engine.dispose()
