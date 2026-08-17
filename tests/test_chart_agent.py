"""Milestone C: the agent's chart-maintenance surface + the review join.

The agent adds no rules of its own — it is a conversational client of the
same audited core the CLI drives. What is tested here is exactly that:
identical outcomes, identical trail, and the guardrails holding.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from hdh.core.models import (
    ChartAuditEvent,
    Condition,
    ConditionStatus,
    EditSource,
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
from hdh.modules.agent.chart_tools import build_chart_tools

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))


@pytest.fixture()
def chart(tmp_path):
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart_agent.db"))
    session = get_session(engine)
    specialty = Specialty(code="FM", name="Family Medicine")
    session.add(specialty)
    session.flush()
    provider = Provider(identifier="NPI7770001", name="Dr. Priya Sharma, MD", specialty_id=specialty.id)
    patient = Patient(
        mrn="MRN00AGENTE",
        first_name="Agent",
        last_name="Edit",
        date_of_birth=date(1969, 4, 4),
        sex=Sex.FEMALE,
    )
    session.add_all([provider, patient])
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 7, 7), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add_all(
        [
            Condition(
                patient_id=patient.id,
                visit_id=visit.id,
                icd10_code="I10",
                description="Essential hypertension",
                status=ConditionStatus.ACTIVE,
            ),
            Prescription(visit_id=visit.id, drug_name="Lisinopril", dose="10mg"),
        ]
    )
    session.commit()
    yield session, patient, visit
    session.close()
    engine.dispose()


def _tool(session, name):
    return next(t for t in build_chart_tools(session) if t.name == name)


def test_toolset_is_amend_void_history_only(chart):
    session, _, _ = chart
    names = {tool.name for tool in build_chart_tools(session)}
    assert names == {"amend_chart_entry", "void_chart_entry", "chart_history"}
    # guardrail 4: deletion is not reachable from the agent at all
    assert not any("purge" in name or "delete" in name for name in names)


def test_agent_amend_previews_then_writes_with_attribution(chart):
    session, patient, _ = chart
    condition = session.query(Condition).first()
    call = _tool(session, "amend_chart_entry").call
    args = {
        "entity": "Condition",
        "row_id": condition.id,
        "changes": json.dumps({"controlled": True}),
        "reason": "Dr. Sharma confirmed control at follow-up",
    }

    preview = json.loads(call({**args, "dry_run": True}))[0]
    assert preview["applied"] and preview["detail"].startswith("[dry run]")
    assert session.query(ChartAuditEvent).count() == 0  # guardrail 3: nothing written

    applied = json.loads(call(args))[0]
    assert applied["applied"] and applied["after"] == {"controlled": True}
    event = session.get(ChartAuditEvent, applied["audit_id"])
    assert event.actor_source is EditSource.AGENT
    assert event.actor_name == "Dr. Priya Sharma, MD"  # named in the reason → attributed
    assert event.provider_id is not None


def test_agent_refusals_come_back_as_outcomes(chart):
    session, _, _ = chart
    condition = session.query(Condition).first()
    call = _tool(session, "amend_chart_entry").call

    no_reason = json.loads(
        call({"entity": "Condition", "row_id": condition.id, "changes": '{"chronic": true}', "reason": ""})
    )[0]
    assert not no_reason["applied"] and "require a reason" in no_reason["detail"]

    bad_json = call(
        {"entity": "Condition", "row_id": condition.id, "changes": "status=resolved", "reason": "x"}
    )
    assert "must be a JSON object" in bad_json

    not_amendable = json.loads(
        call({"entity": "Condition", "row_id": condition.id, "changes": '{"patient_id": 9}', "reason": "x"})
    )[0]
    assert not not_amendable["applied"] and "not amendable" in not_amendable["detail"]

    # the session survived every refusal (tool_guard) and nothing was audited
    assert session.query(ChartAuditEvent).count() == 0


def test_agent_void_cascades_and_history_reads_the_trail(chart):
    session, patient, visit = chart
    voided = json.loads(
        _tool(session, "void_chart_entry").call(
            {"entity": "Visit", "row_id": visit.id, "reason": "duplicate encounter"}
        )
    )[0]
    assert voided["applied"] and "owned rows" in voided["detail"]
    session.expunge_all()
    assert session.query(Visit).count() == 0 and session.query(Prescription).count() == 0

    trail = json.loads(_tool(session, "chart_history").call({"mrn": "MRN00AGENTE"}))
    assert {event["entity"] for event in trail} >= {"Visit", "Condition", "Prescription"}
    assert all(event["source"] == "agent" for event in trail)
    assert trail[0]["reason"]  # every recorded change explains itself

    assert "No patient" in _tool(session, "chart_history").call({"mrn": "MRN00NOBODY"})


def test_agent_and_cli_edits_share_one_trail(chart):
    """The point of putting chartedit in core: same core, same shape."""
    from hdh.core.chartedit import Actor, ChartEdit, EditAction, apply_edits

    session, patient, _ = chart
    condition = session.query(Condition).first()
    apply_edits(
        session,
        Actor(name="arsal", source=EditSource.CLI),
        [ChartEdit("Condition", condition.id, EditAction.AMEND, {"chronic": True}, "charted by hand")],
    )
    _tool(session, "amend_chart_entry").call(
        {
            "entity": "Condition",
            "row_id": condition.id,
            "changes": json.dumps({"status": "resolved"}),
            "reason": "resolved per patient report",
        }
    )
    trail = json.loads(_tool(session, "chart_history").call({"mrn": "MRN00AGENTE"}))
    assert [event["source"] for event in trail] == ["agent", "cli"]  # newest first
    assert all(event["entity"] == "Condition" and event["action"] == "amend" for event in trail)


# ── the join: accepting a review item charts it (design §4) ──────────


def _record_with_flagged_mention(session, visit, text="follow-up"):
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.extract import stub_extractor
    from hdh.modules.comprehension.pipeline import comprehend_note, store_record

    note_text = f"Patient reports {text} today."
    stored = VisitNote(visit_id=visit.id, text=note_text)
    session.add(stored)
    session.flush()
    raw = {"mentions": [{"type": "problem", "text": text, "occurrence": 1, "attributes": []}]}
    comprehended = comprehend_note(session, comprehend_text(note_text, stub_extractor(raw)))
    record_id = store_record(session, stored.id, comprehended)
    session.commit()
    return record_id


def test_accepting_a_review_item_charts_it_through_the_audited_path(chart):
    from sqlalchemy import select

    from hdh.core.models import Base
    from hdh.modules.comprehension.cli import run_review
    from hdh.modules.snomed.loader import run_load

    session, patient, visit = chart
    run_load(session, SNOMED_FIXTURES)
    record_id = _record_with_flagged_mention(session, visit)
    before = session.query(Condition).count()

    run_review(
        session,
        argparse.Namespace(resolve=record_id, decision="accept", icd10="R51.9", mention=None),
    )

    assert session.query(Condition).count() == before + 1
    charted = session.query(Condition).filter(Condition.icd10_code == "R51.9").one()
    assert charted.patient_id == patient.id and charted.visit_id == visit.id

    event = session.query(ChartAuditEvent).filter(ChartAuditEvent.row_id == charted.id).one()
    assert event.action.value == "create" and event.actor_source is EditSource.CLI
    assert f"record #{record_id}" in event.reason  # the human decision, recorded

    records_t = Base.metadata.tables["note_records"]
    status = session.execute(select(records_t.c.status).where(records_t.c.id == record_id)).scalar()
    assert str(status).endswith("complete")


def test_review_resolution_refuses_what_it_cannot_place(chart):
    from hdh.modules.comprehension.cli import run_review
    from hdh.modules.snomed.loader import run_load

    session, _, visit = chart
    run_load(session, SNOMED_FIXTURES)
    record_id = _record_with_flagged_mention(session, visit)

    with pytest.raises(SystemExit, match="only applies to --decision accept"):
        run_review(
            session, argparse.Namespace(resolve=record_id, decision="reject", icd10="R51.9", mention=None)
        )
    with pytest.raises(SystemExit, match="no flagged problem mention"):
        run_review(
            session,
            argparse.Namespace(resolve=record_id, decision="accept", icd10="R51.9", mention="nonexistent"),
        )
    # rejecting without a code stays a plain status flip — nothing charted
    before = session.query(Condition).count()
    run_review(session, argparse.Namespace(resolve=record_id, decision="reject", icd10=None, mention=None))
    assert session.query(Condition).count() == before
