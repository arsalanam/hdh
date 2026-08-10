"""Alembic wiring tests (issue #7): autogenerate sees registry-merged metadata.

The definition of done from the issue: editing a schema module and running
autogenerate must produce a migration that includes the module's extension
columns — which requires ``bootstrap_schema()`` to run inside the Alembic
process (migrations/env.py). These tests drive the Alembic command API
against a throwaway SQLite database and a throwaway versions directory.
"""

import os
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
    cols = {c["name"] for c in inspect(engine).get_columns("diagnoses")}
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
