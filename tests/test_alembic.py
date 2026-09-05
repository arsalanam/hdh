"""Alembic wiring tests (issue #7): autogenerate sees registry-merged metadata.

The definition of done from the issue: editing a schema module and running
autogenerate must produce a migration that includes the module's extension
columns — which requires ``bootstrap_schema()`` to run inside the Alembic
process (migrations/env.py). These tests drive the Alembic command API
against a throwaway SQLite database and a throwaway versions directory.
"""

import os
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def alembic_cfg(tmp_path):
    """Alembic config pointed at a throwaway DB and versions directory."""
    versions = tmp_path / "versions"
    versions.mkdir()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option(
        "version_locations",
        os.pathsep.join([str(REPO_ROOT / "migrations" / "versions"), str(versions)]),
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'alembic_test.db'}")
    return cfg, tmp_path, versions


def test_baseline_upgrade_and_stamp(alembic_cfg):
    """The baseline revision applies cleanly to an empty database."""
    cfg, tmp_path, _versions = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert inspect(engine).has_table("alembic_version")
    engine.dispose()


def test_autogenerate_sees_registry_extensions(alembic_cfg):
    """Autogenerate on an empty DB emits the full registry-merged schema —
    including columns that exist only because a schema module added them."""
    cfg, tmp_path, versions = alembic_cfg
    command.upgrade(cfg, "head")  # stamp baseline first
    command.revision(cfg, message="full schema", autogenerate=True, version_path=str(versions))
    scripts = list(versions.glob("*.py"))
    assert len(scripts) == 1
    body = scripts[0].read_text(encoding="utf-8")
    assert "create_table" in body and "'patients'" in body
    # the ontology module's registry-added columns must be in the diff:
    assert "snomed_code" in body, "bootstrap_schema() did not run in env.py"

    command.upgrade(cfg, "head")
    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    cols = {c["name"] for c in inspect(engine).get_columns("conditions")}
    assert {"icd10_code", "snomed_code", "snomed_display"} <= cols
    engine.dispose()


def test_ensure_columns_steps_aside_under_alembic(alembic_cfg):
    """Once alembic_version exists, the registry's auto-ADD path is inert."""
    cfg, tmp_path, _versions = alembic_cfg
    command.upgrade(cfg, "head")
    from hdh.core.schema_registry import bootstrap_schema

    registry = bootstrap_schema()
    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert registry.ensure_columns(engine) == []
    engine.dispose()


def test_reconcile_migration_repairs_stamped_drift(alembic_cfg):
    """Issue #30: a database stamped before the chart expansion is missing
    columns and the auto-ADD path rightly refuses to touch it — migration
    0003 adds them under Alembic's ownership, idempotently."""
    from sqlalchemy import text

    from hdh.core.schema_registry import bootstrap_schema, reconcile_missing_columns

    cfg, tmp_path, _versions = alembic_cfg
    bootstrap_schema()
    from hdh.core.models import Base

    url = cfg.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # simulate the pre-expansion shape
        conn.execute(text("ALTER TABLE patients DROP COLUMN marital_status"))
        conn.execute(text("ALTER TABLE patients DROP COLUMN language"))
    command.stamp(cfg, "0002")  # stamped BEFORE the reconcile revision existed

    registry = bootstrap_schema()
    assert registry.ensure_columns(engine) == []  # the guard that caused #30

    command.upgrade(cfg, "head")  # 0003 reconciles

    inspector = inspect(engine)
    assert "marital_status" in {c["name"] for c in inspector.get_columns("patients")}
    assert "language" in {c["name"] for c in inspector.get_columns("patients")}
    with engine.begin() as conn:  # idempotent: a second reconcile is a no-op
        assert reconcile_missing_columns(conn) == []
    engine.dispose()


#: The pre-0006 shape of the two tables that gained a request pointer.
#: Written out rather than produced by dropping columns, because SQLite
#: refuses to DROP a column named in a foreign-key clause — and rebuilding
#: the old shape is a truer simulation of an old database anyway.
_PRE_ORDERS_TABLES = {
    "lab_results": """
        CREATE TABLE lab_results (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER NOT NULL,
            test_name VARCHAR(100) NOT NULL,
            value FLOAT,
            unit VARCHAR(20),
            reference_low FLOAT,
            reference_high FLOAT,
            status VARCHAR(8) NOT NULL,
            loinc_code VARCHAR(10),
            voided_at DATETIME,
            FOREIGN KEY(visit_id) REFERENCES visits (id)
        )""",
    "prescriptions": """
        CREATE TABLE prescriptions (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER NOT NULL,
            drug_name VARCHAR(100) NOT NULL,
            drug_class VARCHAR(80),
            dose VARCHAR(40),
            frequency VARCHAR(40),
            duration_days INTEGER,
            refills INTEGER,
            is_new BOOLEAN,
            voided_at DATETIME,
            FOREIGN KEY(visit_id) REFERENCES visits (id)
        )""",
}


def test_0006_adds_service_requests_to_a_database_stamped_at_0005(alembic_cfg):
    """A database built before orders existed has neither the table nor the
    columns that point at it. ``create_all`` would add the table but never
    ALTER ``lab_results``, so the columns are the half that needs the
    migration — the stale-schema failure class issue #30 was about."""
    from sqlalchemy import text

    from hdh.core.schema_registry import bootstrap_schema

    cfg, _tmp_path, _versions = alembic_cfg
    bootstrap_schema()
    from hdh.core.models import Base

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # roll the schema back to before orders
        conn.execute(text("DROP TABLE service_requests"))
        for table, ddl in _PRE_ORDERS_TABLES.items():
            conn.execute(text(f"DROP TABLE {table}"))
            conn.execute(text(ddl))
    command.stamp(cfg, "0005")

    inspector = inspect(engine)
    assert not inspector.has_table("service_requests")
    assert "request_id" not in {c["name"] for c in inspector.get_columns("lab_results")}

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert inspector.has_table("service_requests")
    lab_columns = {c["name"] for c in inspector.get_columns("lab_results")}
    assert {"request_id", "value_text", "comparator"} <= lab_columns
    assert "request_id" in {c["name"] for c in inspector.get_columns("prescriptions")}

    command.upgrade(cfg, "head")  # idempotent: nothing left to add
    engine.dispose()


def test_0007_turns_follow_up_days_into_orders_without_losing_it(alembic_cfg):
    """The scalar becomes a FOLLOW_UP request (#59, design §9 Q5).

    The point of this migration is data, not shape: a chart that has been
    asking patients back for years must not lose that when the column goes.
    So this builds the OLD shape with real values, migrates, and checks the
    interval survives — including a round trip back."""
    from sqlalchemy import text

    from hdh.core.schema_registry import bootstrap_schema

    cfg, _tmp_path, _versions = alembic_cfg
    bootstrap_schema()
    from hdh.core.models import Base

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE visits ADD COLUMN follow_up_days INTEGER"))
        conn.execute(
            text(
                "INSERT INTO patients (id, mrn, first_name, last_name, date_of_birth, sex) "
                "VALUES (1, 'MRN-0007', 'Old', 'Chart', '1970-01-01', 'FEMALE')"
            )
        )
        for visit_id, day, days in ((1, "2026-01-10", 90), (2, "2026-02-20", 14), (3, "2026-03-05", None)):
            conn.execute(
                text(
                    "INSERT INTO visits (id, patient_id, visit_date, visit_type, follow_up_days) "
                    "VALUES (:i, 1, :d, 'FOLLOW_UP', :f)"
                ),
                {"i": visit_id, "d": day, "f": days},
            )
    command.stamp(cfg, "0006")

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert "follow_up_days" not in {c["name"] for c in inspector.get_columns("visits")}
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT visit_id, requested_date, occurrence_date, origin FROM service_requests "
                "WHERE kind = 'FOLLOW_UP' ORDER BY visit_id"
            )
        ).all()
    # the PRN visit (None) must not have invented an order
    assert [r.visit_id for r in rows] == [1, 2]
    intervals = {
        r.visit_id: (
            date.fromisoformat(str(r.occurrence_date)) - date.fromisoformat(str(r.requested_date))
        ).days
        for r in rows
    }
    assert intervals == {1: 90, 2: 14}
    assert {r.origin for r in rows} == {"GENERATED"}

    command.downgrade(cfg, "0006")
    inspector = inspect(engine)
    assert "follow_up_days" in {c["name"] for c in inspector.get_columns("visits")}
    with engine.begin() as conn:
        restored = dict(conn.execute(text("SELECT id, follow_up_days FROM visits")).all())
    assert restored == {1: 90, 2: 14, 3: None}
    engine.dispose()


def test_0008_adds_the_interchange_review_queue(alembic_cfg):
    """The simple half of the guarded pattern: one new table, no column
    changes. `create_all` builds it on a fresh database; this covers one
    that already exists."""
    from sqlalchemy import text

    from hdh.core.schema_registry import bootstrap_schema

    cfg, _tmp_path, _versions = alembic_cfg
    bootstrap_schema()
    from hdh.core.models import Base

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE rejected_results"))
    command.stamp(cfg, "0007")
    assert not inspect(engine).has_table("rejected_results")

    command.upgrade(cfg, "head")

    assert inspect(engine).has_table("rejected_results")
    command.upgrade(cfg, "head")  # idempotent
    command.downgrade(cfg, "0007")
    assert not inspect(engine).has_table("rejected_results")
    engine.dispose()


def test_0009_adds_code_columns_to_the_medication_rows(alembic_cfg):
    """A code annotates what was written, so these are additive columns
    beside the free text — and `create_all` never ALTERs an existing
    table, which is the half a migration exists for."""
    from sqlalchemy import text

    from hdh.core.schema_registry import bootstrap_schema

    cfg, _tmp_path, _versions = alembic_cfg
    bootstrap_schema()
    from hdh.core.models import Base

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # roll back to the pre-0009 shape
        for table in ("prescriptions", "medication_statements"):
            for column in ("code_system", "code"):
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    command.stamp(cfg, "0008")

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    for table in ("prescriptions", "medication_statements"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert {"code_system", "code"} <= columns, table
    command.upgrade(cfg, "head")  # idempotent
    engine.dispose()


# ── 0020: the person tables, and the backfill that nearly lost 164 rows ──


def _person_db(cfg):
    """A database with the real tables, stamped just before 0020.

    `create_all` builds the schema (the migrations do not create `patients`
    — the registry does), then the tables 0020 owns are dropped so the
    migration creates and backfills them the way it would on a real
    database.
    """
    from sqlalchemy import text as sql

    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    from hdh.core.models import Base

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in ("patient_coverages", "patient_contacts", "patient_addresses", "patient_identifiers"):
            conn.execute(sql(f"DROP TABLE IF EXISTS {table}"))
    command.stamp(cfg, "0019")
    return engine


def _seed_patient(engine, **overrides):
    from sqlalchemy import text as sql

    values = {
        "mrn": "MRN00000001",
        "date_of_birth": "1970-01-01",
        "sex": "F",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "address": "1 Analytical Way",
        "city": "London",
        "state": "LDN",
        "zip_code": "E1",
        "phone": "0100 000000",
        "email": "ada@example.test",
        "insurance_name": "Aetna",
        "insurance_id": "ABC-123",
    }
    values.update(overrides)
    columns = ", ".join(values)
    binds = ", ".join(f":{k}" for k in values)
    with engine.begin() as conn:
        conn.execute(sql(f"INSERT INTO patients ({columns}) VALUES ({binds})"), values)


def _counts(engine, table, column):
    from sqlalchemy import text as sql

    with engine.connect() as conn:
        return dict(conn.execute(sql(f"SELECT {column}, count(*) FROM {table} GROUP BY {column}")).all())


def test_0020_backfills_every_contact_kind_not_just_the_first(alembic_cfg):
    """Two statements write to `patient_contacts`. The first version of the
    backfill guarded on "is this table empty", so inserting the phone rows
    made it non-empty and the email insert was skipped — 164 email contacts
    went missing on the live database, and the table looked plausibly full
    at exactly one row per patient."""
    cfg, tmp_path, _versions = alembic_cfg
    engine = _person_db(cfg)
    _seed_patient(engine)
    command.upgrade(cfg, "0020")
    assert _counts(engine, "patient_contacts", "system") == {"phone": 1, "email": 1}


def test_0020_backfills_the_other_three_tables(alembic_cfg):
    cfg, tmp_path, _versions = alembic_cfg
    engine = _person_db(cfg)
    _seed_patient(engine)
    command.upgrade(cfg, "0020")
    assert _counts(engine, "patient_identifiers", "kind") == {"mrn": 1}
    assert _counts(engine, "patient_addresses", "use") == {"home": 1}
    assert _counts(engine, "patient_coverages", "rank") == {1: 1}


def test_0020_skips_blank_values(alembic_cfg):
    """An empty email is not a contact, and a row saying so is worse than no
    row because it reads as a recorded fact."""
    cfg, tmp_path, _versions = alembic_cfg
    engine = _person_db(cfg)
    _seed_patient(engine, email="", insurance_name="")
    command.upgrade(cfg, "0020")
    assert _counts(engine, "patient_contacts", "system") == {"phone": 1}
    assert _counts(engine, "patient_coverages", "rank") == {}


def test_0020_downgrade_then_upgrade_does_not_duplicate(alembic_cfg):
    """The repair path actually taken on the live database."""
    cfg, tmp_path, _versions = alembic_cfg
    engine = _person_db(cfg)
    _seed_patient(engine)
    command.upgrade(cfg, "0020")
    command.downgrade(cfg, "0019")
    command.upgrade(cfg, "0020")
    assert _counts(engine, "patient_contacts", "system") == {"phone": 1, "email": 1}


def test_0021_adds_the_person_columns(alembic_cfg):
    cfg, tmp_path, _versions = alembic_cfg
    engine = _person_db(cfg)
    command.upgrade(cfg, "head")
    columns = {c["name"] for c in inspect(engine).get_columns("patients")}
    assert {"preferred_name", "gender_identity", "pronouns", "registered_provider_id"} <= columns
