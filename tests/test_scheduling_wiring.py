"""The `scheduling` intent is wired end to end: registered in the classifier
schema, the topic guard, and the per-intent tool/table allow-lists, and the
executor actually hands those tools over for the intent. No LLM — these are the
static wiring assertions that catch a half-registered intent."""

from __future__ import annotations

import pytest

from hdh.modules.agent.pipeline.gateway import INTENT_SCHEMA, INTENT_TABLES, INTENT_TOOLS
from hdh.modules.agent.pipeline.state import DEFAULT_ALLOWED_TOPICS

_SCHEDULING_TOOLS = {
    "find_appointments",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "link_encounter_to_appointment",
}


def test_scheduling_is_a_known_intent():
    assert "scheduling" in INTENT_SCHEMA["properties"]["intent"]["enum"]


def test_scheduling_tool_and_table_allow_lists_exist():
    assert _SCHEDULING_TOOLS <= INTENT_TOOLS["scheduling"]
    assert "appointments" in INTENT_TABLES["scheduling"]


def test_topic_guard_admits_appointments():
    assert any("appointment" in topic.lower() for topic in DEFAULT_ALLOWED_TOPICS)


@pytest.fixture
def sched_db():
    from hdh.core.generators import build_dataset
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    session = get_session(get_engine(":memory:"))
    build_dataset(session, n_patients=2, years_of_history=1, verbose=False)
    yield session
    session.close()


def test_executor_exposes_scheduling_tools_for_the_intent(sched_db):
    """build_tools, given the scheduling intent's allow-list, surfaces the
    scheduling tools — the path the gateway uses to select tools per intent."""
    from hdh.modules.agent.tools import build_tools

    names = {t.name for t in build_tools(sched_db, include=INTENT_TOOLS["scheduling"], identity=None)}
    assert _SCHEDULING_TOOLS <= names
