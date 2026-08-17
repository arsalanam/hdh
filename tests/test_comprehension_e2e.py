"""Scripted agent scenarios (design §14.5).

Every flow here was first run by hand in an agent chat during the
comprehension and chart-maintenance arcs. Frozen as scripts, they cost
zero API calls and zero minutes — and they cover the one thing unit
tests kept missing: what happens when the *sequence* is wrong.

The guardrail probe is the important one. Live testing proved the agent
escalates to raw SQL when its sanctioned tools can't do the job; this
pins both halves of that story — the write is refused, and the sanctioned
path succeeds.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from hdh.core.models import (
    Condition,
    Patient,
    Prescription,
    Provider,
    Sex,
    Specialty,
    Visit,
    VisitNote,
    VisitType,
    get_engine,
    get_session,
)
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.agent_tools import build_comprehension_tools
from hdh.modules.comprehension.extract import stub_extractor

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

ICD = "B99.9"
NOTE = "Patient seen today. Chronic blorbitis remains active. Start Apixaban 5mg BID. BP 141/90 mmHg."
RAW = {
    "mentions": [
        {"type": "problem", "text": "Chronic blorbitis", "occurrence": 1, "attributes": []},
        {
            "type": "medication",
            "text": "Apixaban",
            "occurrence": 1,
            "attributes": [{"kind": "dose", "text": "5mg", "occurrence": 1}],
        },
        {
            "type": "lab_vital",
            "text": "BP",
            "occurrence": 1,
            "attributes": [{"kind": "value", "text": "141/90", "occurrence": 1}],
        },
    ]
}


@pytest.fixture()
def clinic(tmp_path):
    """A chart with a provider, a billing edge, and one stored note — the
    minimum for the agent toolset to offer itself."""
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "e2e.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    tables = Base.metadata.tables
    session.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": f"icd10cm:{ICD}",
                "ontology": "icd10cm",
                "code": ICD,
                "kind": "leaf",
                "display": "Blorbitis",
                "is_billable": True,
            }
        ],
    )
    session.execute(
        insert(tables["ontology_edges"]),
        [
            {
                "source_id": f"icd10cm:{ICD}",
                "target_id": f"snomed_ct:{fx.CHRONIC_BLORBITIS}",
                "edge_type": "maps_to",
                "authority": "CURATED_DEMO",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    specialty = Specialty(code="FM", name="Family Medicine")
    session.add(specialty)
    session.flush()
    session.add(Provider(identifier="NPI5550001", name="Dr. Priya Sharma, MD", specialty_id=specialty.id))
    patient = Patient(
        mrn="MRN00E2E001", first_name="End", last_name="Toend", date_of_birth=date(1972, 2, 2), sex=Sex.FEMALE
    )
    session.add(patient)
    session.flush()
    seed = Visit(patient_id=patient.id, visit_date=date(2026, 1, 1), visit_type=VisitType.FOLLOW_UP)
    session.add(seed)
    session.flush()
    session.add(VisitNote(visit_id=seed.id, text="Seed note so the toolset offers itself."))
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


def _tool(session, name):
    return next(
        t for t in build_comprehension_tools(session, extractor=stub_extractor(RAW)) if t.name == name
    )


def _chart_tool(session, name):
    from hdh.modules.agent.chart_tools import build_chart_tools

    return next(t for t in build_chart_tools(session) if t.name == name)


# ── scenario 1: a provider charts a note by talking to the agent ──────


def test_scenario_provider_charts_a_note_from_chat(clinic):
    session, patient = clinic
    payload = json.loads(
        _tool(session, "apply_note").call(
            {"mrn": patient.mrn, "note_text": NOTE, "visit_date": "yesterday", "provider": "Priya Sharma"}
        )
    )
    assert payload["created_visit"] is True
    assert payload["visit_date"] == str(date.today() - timedelta(days=1))
    assert payload["provider"] == "Dr. Priya Sharma, MD"
    assert payload["note_record_id"] > 0
    assert not payload["review_items"], "a fully-mapped note should chart without review"

    actions = {(v["action"], v["kind"]) for v in payload["verdicts"]}
    assert {("new", "condition"), ("new", "medication"), ("new", "vitals")} <= actions

    stored = session.query(VisitNote).filter(VisitNote.text == NOTE).one()
    assert stored.visit.provider.name == "Dr. Priya Sharma, MD"  # provenance


# ── scenario 2: the addendum that used to duplicate the encounter ─────


def test_scenario_addendum_reconciles_into_the_same_visit(clinic):
    session, patient = clinic
    call = _tool(session, "apply_note").call
    first = json.loads(call({"mrn": patient.mrn, "note_text": NOTE, "visit_date": "2026-09-01"}))
    second = json.loads(
        call({"mrn": patient.mrn, "note_text": "Addendum: " + NOTE, "visit_date": "2026-09-01"})
    )
    assert second["created_visit"] is False and second["visit_id"] == first["visit_id"]

    actions = {(v["action"], v["kind"]) for v in second["verdicts"]}
    assert ("confirmed", "condition") in actions and ("confirmed", "medication") in actions
    assert session.query(Visit).filter(Visit.visit_date == date(2026, 9, 1)).count() == 1
    assert session.query(Condition).count() == 1 and session.query(Prescription).count() == 1


# ── scenario 3: refuse-don't-guess, end to end ───────────────────────


def test_scenario_unmapped_complaint_is_surfaced_not_written(clinic):
    session, patient = clinic
    raw = {"mentions": [{"type": "problem", "text": "acute blorbitis", "occurrence": 1, "attributes": []}]}
    tools = build_comprehension_tools(session, extractor=stub_extractor(raw))
    payload = json.loads(
        next(t for t in tools if t.name == "apply_note").call(
            {
                "mrn": patient.mrn,
                "note_text": "Patient reports acute blorbitis today.",
                "visit_date": "2026-09-05",
            }
        )
    )
    assert payload["review_items"], "an unmapped complaint must reach a human"
    assert any("no ICD billing mapping" in item for item in payload["review_items"])
    assert session.query(Condition).count() == 0  # nothing written


# ── scenario 4: the guardrail probe, both halves ─────────────────────


def test_scenario_agent_cannot_write_through_sql_but_can_through_the_tool(clinic):
    """Live testing's defining moment: asked to fix the chart, the agent
    reached for raw SQL. The guard refused it. Now there is a sanctioned
    path that succeeds — and the refusal still holds."""
    from hdh.modules.agent.tools import build_tools

    session, patient = clinic
    charted = json.loads(
        _tool(session, "apply_note").call({"mrn": patient.mrn, "note_text": NOTE, "visit_date": "2026-09-09"})
    )
    condition = session.query(Condition).one()

    # half one: the read-only guard refuses the write, and the session survives
    query_database = next(t for t in build_tools(session) if t.name == "query_database")
    refused = query_database.call({"sql": f"UPDATE conditions SET icd10_code='X99' WHERE id={condition.id}"})
    assert "single SELECT statement is allowed" in refused, refused
    session.expire_all()
    assert session.get(Condition, condition.id).icd10_code == ICD  # unchanged

    # half two: the sanctioned path does the same job, audited
    from hdh.core.models import ChartAuditEvent

    outcome = json.loads(
        _chart_tool(session, "amend_chart_entry").call(
            {
                "entity": "Condition",
                "row_id": condition.id,
                "changes": json.dumps({"status": "resolved"}),
                "reason": "resolved at follow-up per Dr. Sharma",
            }
        )
    )[0]
    assert outcome["applied"] and outcome["audit_id"]
    event = session.get(ChartAuditEvent, outcome["audit_id"])
    assert event.entity == "Condition" and event.reason
    assert charted["visit_id"] > 0


# ── scenario 5: the inputs a chat will actually get wrong ────────────


@pytest.mark.parametrize(
    "args,fragment",
    [
        ({"mrn": "MRN00NOBODY", "note_text": NOTE}, "No patient"),
        ({"mrn": "MRN00E2E001", "note_text": NOTE, "provider": "Dr. Nobody"}, None),
    ],
)
def test_scenario_bad_inputs_answer_instead_of_raising(clinic, args, fragment):
    session, _ = clinic
    result = _tool(session, "apply_note").call(args)
    if fragment:
        assert fragment in result
    else:
        payload = json.loads(result)
        assert payload["provider"] is None  # unknown provider: charted, unattributed
        assert payload["visit_id"] > 0
