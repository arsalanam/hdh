"""Entities describe their meaning, not only their structure (#93).

The columns in the SQL tool's description were always generated from live
ORM metadata and could not drift. The sentence saying what a table is *for*
was a hand-written paragraph inside `agent/tools.py`, about tables that
module owns none of — and it went four behind:

    service_requests   ✅ in the column list   ❌ in the prose
    note_records       ✅                      ❌
    rejected_results   ✅                      ❌
    care_plan_records  ✅                      ❌

So the agent could see `service_requests(...)` and had no idea it was the
orders table, how it joined, or that `status` is an enum — and a wrong guess
there produces confident SQL over the wrong table, which is the failure mode
this project spends its effort preventing everywhere else.

It drifted in the only way it could: every new module shipped an entity
without shipping the sentence that explains it. The fix is not to write the
missing four paragraphs; it is to make the meaning travel with the entity,
and to fail when it does not.
"""

from __future__ import annotations

import json
import pathlib

import pytest


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()


def _semantics():
    from hdh.core.schema_registry import table_semantics

    return table_semantics()


# ── the structural guarantee ─────────────────────────────────────────────


def test_every_table_an_intent_exposes_has_meaning():
    """The gate. Adding a table to INTENT_TABLES without declaring what it
    means now fails here rather than silently handing the agent a column
    list to guess from."""
    from hdh.modules.agent.pipeline.gateway import INTENT_TABLES

    exposed = {table for tables in INTENT_TABLES.values() for table in tables}
    missing = sorted(exposed - set(_semantics()))
    assert not missing, f"exposed to the agent with no declared meaning: {missing}"


def test_the_four_tables_that_drifted_are_covered():
    """Named individually, because they are the evidence. A general rule
    that happened to pass while these were still bare would be no fix."""
    meanings = _semantics()
    for table in ("service_requests", "note_records", "care_plan_records"):
        assert table in meanings, table
        assert meanings[table].get("purpose"), f"{table} has a block but says nothing"


def test_every_declared_meaning_names_a_real_table():
    """A stale entry is worse than a missing one: it reads as covered."""
    from hdh.core.models import Base

    unknown = sorted(set(_semantics()) - set(Base.metadata.tables))
    assert not unknown, f"semantics declared for tables that do not exist: {unknown}"


def test_a_purpose_is_required_of_every_declaration():
    for table, block in _semantics().items():
        assert block.get("purpose"), f"{table} declares semantics with no purpose"


# ── where the meaning lives ──────────────────────────────────────────────


def test_module_entities_carry_their_own_meaning():
    """The structural argument. If the care-plan tables were described in
    core, or in the agent module, the next module would drift the same way
    this one did."""
    spec = json.loads(
        pathlib.Path("src/hdh/modules/careplan/schema/entities/care_plan_record.json").read_text(
            encoding="utf-8"
        )
    )
    assert spec["semantics"]["purpose"]


def test_base_tables_are_described_beside_the_models():
    """`models.py` classes have no entity JSON to carry a block, so core
    declares theirs in one file next to them — not in the agent."""
    from hdh.core.schema_registry import BASE_SEMANTICS

    raw = json.loads(BASE_SEMANTICS.read_text(encoding="utf-8"))
    assert raw["conditions"]["purpose"]


def test_the_agent_no_longer_narrates_tables_it_does_not_own():
    """The paragraph is gone, and cannot come back unnoticed: it named
    tables in prose, so prose naming tables is what this looks for."""
    import inspect

    from hdh.modules.agent import tools

    source = inspect.getsource(tools._sql_tool_description)
    for owned_elsewhere in ("conditions.patient_id", "ontology_concepts.path", "FOLLOW_UP"):
        assert owned_elsewhere not in source, (
            f"{owned_elsewhere!r} is back in the tool description — "
            "that knowledge belongs to whoever declared the entity"
        )


# ── what the agent actually receives ─────────────────────────────────────


def test_the_description_carries_purpose_and_columns_together():
    from hdh.modules.agent.tools import _semantic_schema

    rendered = _semantic_schema(("care_plan_records",))
    assert "care_plan_records(" in rendered, "the generated column list is still there"
    assert "A saved care plan" in rendered, "and the meaning beside it"


def test_the_counterintuitive_column_is_explained():
    """`supersedes_id` is the one an agent would get wrong by reasoning: a
    superseded plan keeps the status it was approved with, so the obvious
    query — filter on status — returns a plan that is no longer in force."""
    from hdh.modules.agent.tools import _semantic_schema

    rendered = _semantic_schema(("care_plan_records",))
    assert "does NOT change the old plan's status" in rendered
    assert "do not filter on status" in rendered


def test_intent_scoping_still_narrows_what_is_sent():
    """Richer descriptions make the narrowing matter more, not less."""
    from hdh.modules.agent.tools import _semantic_schema

    scoped = _semantic_schema(("patients",))
    assert "care_plan_records" not in scoped
    assert len(scoped) < len(_semantic_schema(None))


def test_a_table_with_no_declared_meaning_still_renders_its_columns():
    """Degrading to today's behaviour, not to nothing. The gate covers what
    an intent exposes; the fallback set is every table, and a bare one there
    must not vanish from the schema."""
    from hdh.core.models import Base
    from hdh.modules.agent.tools import _semantic_schema

    bare = sorted(set(Base.metadata.tables) - set(_semantics()))
    if not bare:
        pytest.skip("every table is described")
    rendered = _semantic_schema(None)
    assert f"{bare[0]}(" in rendered


def test_the_model_is_told_when_a_table_is_unexplained():
    """Silence would read as 'nothing worth saying' rather than 'unknown'."""
    from hdh.modules.agent.tools import _sql_tool_description

    assert "carry columns only" in _sql_tool_description(None, "postgresql")
