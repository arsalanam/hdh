"""Comprehension milestone D: the agent as prime consumer + the review
loop. Offline throughout: the injectable extractor keeps the LLM out,
the fixture SNOMED world supplies the codes."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from hdh.core.models import Patient, Sex, Visit, VisitNote, VisitType, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.agent_tools import build_comprehension_tools
from hdh.modules.comprehension.extract import stub_extractor

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

NOTE = "Chronic blorbitis follow-up. She denies glimmer fever. Continue Apixaban 5mg BID."
RAW = {
    "mentions": [
        {"type": "problem", "text": "Chronic blorbitis", "occurrence": 1, "attributes": []},
        {"type": "problem", "text": "glimmer fever", "occurrence": 1, "attributes": []},
        {
            "type": "medication",
            "text": "Apixaban",
            "occurrence": 1,
            "attributes": [
                {"kind": "dose", "text": "5mg", "occurrence": 1},
            ],
        },
    ],
    "relations": [],
    "shared_triggers": [],
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("agent_comp") / "world.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    patient = Patient(
        mrn="MRN00AGENTC",
        first_name="Prime",
        last_name="Consumer",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 15), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add(VisitNote(visit_id=visit.id, text=NOTE))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_toolset_registers_three_tools(world):
    names = {t.name for t in build_comprehension_tools(world)}
    assert names == {"comprehend_note", "get_note_record", "search_note_mentions", "apply_note"}


def test_agent_build_tools_includes_comprehension(world):
    from hdh.modules.agent.tools import build_tools

    names = {t.name for t in build_tools(world)}
    assert "comprehend_note" in names and "search_note_mentions" in names


def test_comprehend_note_tool_returns_grounded_json(world):
    tools = build_comprehension_tools(world, extractor=stub_extractor(RAW))
    payload = json.loads(
        _tool(tools, "comprehend_note").call({"mrn": "MRN00AGENTC", "visit_date": "2026-08-15"})
    )
    by_text = {m["text"]: m for m in payload["mentions"]}
    assert by_text["Chronic blorbitis"]["code"]["code"] == fx.CHRONIC_BLORBITIS
    assert by_text["glimmer fever"]["assertion"] == "negated"  # "denies" trigger
    assert by_text["Apixaban"]["code"]["system"] == "drug-catalog"

    missing = _tool(tools, "comprehend_note").call({"mrn": "MRN00AGENTC", "visit_date": "1999-01-01"})
    assert "No stored note" in missing


def test_get_note_record_and_search_read_stored_records(world):
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.pipeline import comprehend_note, store_record

    stored = world.query(VisitNote).first()
    comprehended = comprehend_note(world, comprehend_text(NOTE, stub_extractor(RAW)))
    store_record(world, stored.id, comprehended)

    tools = build_comprehension_tools(world, extractor=stub_extractor(RAW))
    record = json.loads(
        _tool(tools, "get_note_record").call({"mrn": "MRN00AGENTC", "visit_date": "2026-08-15"})
    )
    assert record["pipeline_version"] == "0.2"
    assert any(m["concept_id"] == f"snomed_ct:{fx.CHRONIC_BLORBITIS}" for m in record["mentions"])

    hits = json.loads(_tool(tools, "search_note_mentions").call({"snomed_code": fx.CHRONIC_BLORBITIS}))
    assert hits[0]["mrn"] == "MRN00AGENTC"
    negated = json.loads(_tool(tools, "search_note_mentions").call({"text": "glimmer"}))
    assert negated[0]["assertion"] == "negated"  # assertion travels with the stored mention


def test_review_queue_lists_and_resolves(world):
    from sqlalchemy import select

    from hdh.core.models import Base
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.pipeline import comprehend_note, store_record

    # an unlinkable mention → low confidence → needs_review record
    raw = {"mentions": [{"type": "problem", "text": "follow-up", "occurrence": 1, "attributes": []}]}
    stored = world.query(VisitNote).first()
    bad = comprehend_note(world, comprehend_text(NOTE, stub_extractor(raw)))
    record_id = store_record(world, stored.id, bad)

    import argparse

    from hdh.modules.comprehension.cli import run_review

    run_review(world, argparse.Namespace(resolve=None, decision="accept"))  # listing must not raise
    run_review(world, argparse.Namespace(resolve=record_id, decision="accept"))
    records_t = Base.metadata.tables["note_records"]
    status = world.execute(select(records_t.c.status).where(records_t.c.id == record_id)).scalar()
    assert str(status).endswith("complete")


def test_apply_note_tool_charts_free_text(world):
    from hdh.core.models import Provider, Specialty, VisitNote

    if world.query(Provider).first() is None:
        spec = Specialty(code="FM", name="Family Medicine")
        world.add(spec)
        world.flush()
        world.add(Provider(identifier="NPI9990001", name="Dr. Priya Sharma, MD", specialty_id=spec.id))
        world.commit()

    note_text = "Acute blorbitis today. BP 141/90 mmHg. Start Apixaban 5mg BID."
    raw = {
        "mentions": [
            {"type": "problem", "text": "Acute blorbitis", "occurrence": 1, "attributes": []},
            {
                "type": "lab_vital",
                "text": "BP",
                "occurrence": 1,
                "attributes": [{"kind": "value", "text": "141/90", "occurrence": 1}],
            },
            {
                "type": "medication",
                "text": "Apixaban",
                "occurrence": 1,
                "attributes": [{"kind": "dose", "text": "5mg", "occurrence": 1}],
            },
        ],
    }
    tools = build_comprehension_tools(world, extractor=stub_extractor(raw))
    payload = json.loads(
        _tool(tools, "apply_note").call(
            {
                "mrn": "MRN00AGENTC",
                "note_text": note_text,
                "visit_date": "yesterday",
                "provider": "Priya Sharma",
            }
        )
    )
    assert payload["provider"] == "Dr. Priya Sharma, MD"
    assert payload["note_record_id"] > 0
    actions = {(v["action"], v["kind"]) for v in payload["verdicts"]}
    assert ("new", "medication") in actions and ("new", "vitals") in actions
    # acute blorbitis has no maps_to billing edge here -> surfaced for review
    assert any("no ICD billing mapping" in item for item in payload["review_items"])
    # the pasted note persisted as the visit's VisitNote (provenance)
    stored = world.query(VisitNote).filter(VisitNote.text == note_text).first()
    assert stored is not None and stored.visit.provider.name == "Dr. Priya Sharma, MD"


def test_apply_note_addendum_reconciles_into_same_date_visit(world):
    """Same patient + same date = the same encounter — an addendum note
    attaches to the existing visit, never spawns a duplicate one."""
    raw = {
        "mentions": [
            {
                "type": "medication",
                "text": "Apixaban",
                "occurrence": 1,
                "attributes": [{"kind": "dose", "text": "5mg", "occurrence": 1}],
            },
        ],
    }
    tools = build_comprehension_tools(world, extractor=stub_extractor(raw))
    call = _tool(tools, "apply_note").call
    first = json.loads(
        call({"mrn": "MRN00AGENTC", "note_text": "Start Apixaban 5mg BID.", "visit_date": "2026-08-20"})
    )
    assert first["created_visit"] is True
    second = json.loads(
        call({"mrn": "MRN00AGENTC", "note_text": "Addendum: Apixaban 5mg BID.", "visit_date": "2026-08-20"})
    )
    assert second["created_visit"] is False
    assert second["visit_id"] == first["visit_id"]
    # the medication reconciles as confirmed, not duplicated
    actions = {(v["action"], v["kind"]) for v in second["verdicts"]}
    assert ("confirmed", "medication") in actions
    # both notes hang off the one visit (provenance for the addendum too)
    notes = world.query(VisitNote).filter(VisitNote.visit_id == first["visit_id"]).count()
    assert notes == 2
