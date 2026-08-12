"""Milestone E tests: the ICD toolset as the agent sees it.

The tools are plain functions returning JSON strings, so the whole coding
surface tests offline — including the flagship demo's data path: linked
diagnoses joined to graph laterality, and Excludes1 conflicts detected
across one patient's codes, deterministically.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from hdh.core.models import Base, Condition, Patient, Sex, Visit, VisitType, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.icd10cm.loader import run_load

FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"


@pytest.fixture(scope="module")
def coding_session(tmp_path_factory):
    """Catalog + two fracture patients (left and right ulna) linked to it."""
    bootstrap_schema()
    db = tmp_path_factory.mktemp("agent") / "coding.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    run_load(session, FIXTURES, 2026)
    for mrn, code, side in (("MRN20000001", "S52.001A", "right"), ("MRN20000002", "S52.002A", "left")):
        patient = Patient(
            mrn=mrn,
            first_name=side.title(),
            last_name="Fracture",
            date_of_birth=date(1970, 1, 1),
            sex=Sex.FEMALE,
        )
        visit = Visit(patient=patient, visit_date=date(2026, 1, 15), visit_type=VisitType.URGENT)
        session.add_all(
            [patient, visit, Condition(patient=patient, visit=visit, icd10_code=code, description=code)]
        )
    session.commit()
    from hdh.modules.icd10cm.cli import _cmd_link

    _cmd_link(session)
    yield session
    session.close()
    engine.dispose()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_icd_tools_register_and_filter(coding_session):
    """build_tools exposes the ICD set; the coding intent filters to it."""
    from hdh.modules.agent.pipeline.gateway import INTENT_SCHEMA, INTENT_TOOLS
    from hdh.modules.agent.tools import build_tools

    tools = build_tools(coding_session)
    names = {t.name for t in tools}
    assert {"icd_codify", "icd_lookup", "icd_search", "icd_pattern"} <= names

    coding_tools = build_tools(coding_session, include=INTENT_TOOLS["coding"])
    assert {t.name for t in coding_tools} == INTENT_TOOLS["coding"]
    assert "coding" in INTENT_SCHEMA["properties"]["intent"]["enum"]


def test_icd_codify_tool_asks_not_guesses(coding_session):
    """The tool surfaces unstated axes for the agent's follow-up question."""
    from hdh.modules.icd10cm.agent_tools import build_icd_tools

    codify = _tool(build_icd_tools(coding_session), "icd_codify")
    payload = json.loads(
        codify.call({"terms": "fracture medial malleolus", "laterality": "left", "encounter": "initial"})
    )
    top = payload["candidates"][0]
    assert top["code"].startswith("S82.5")
    assert "laterality" in top["matched"]
    # candidates split displaced/nondisplaced and the caller never said —
    # the tool tells the agent to ask, not guess
    assert "displacement" in payload["ask_about"]


def test_icd_pattern_tool_feedback_loop(coding_session):
    """Invalid patterns return actionable feedback instead of raising."""
    from hdh.modules.icd10cm.agent_tools import build_icd_tools

    pattern = _tool(build_icd_tools(coding_session), "icd_pattern")
    feedback = pattern.call({"pattern_json": json.dumps({"anchor": {"terms": "x"}, "sql": "DROP"})})
    assert feedback.startswith("Invalid pattern:") and "unknown pattern keys" in feedback
    hits = json.loads(
        pattern.call(
            {
                "pattern_json": json.dumps(
                    {"anchor": {"code": "S52.001A"}, "traverse": [{"edge": "contralateral"}]}
                )
            }
        )
    )
    assert [h["code"] for h in hits] == ["S52.002A"]


def test_flagship_data_path_laterality_over_patients(coding_session):
    """The flagship demo's join: linked diagnoses × graph laterality."""
    concepts_t = Base.metadata.tables["ontology_concepts"]
    rows = coding_session.execute(
        select(concepts_t.c.laterality, Condition.icd10_code)
        .join(concepts_t, Condition.__table__.c.concept_id == concepts_t.c.id)
        .where(concepts_t.c.laterality.isnot(None))
    ).all()
    counts = {side: 0 for side in ("1", "2")}
    for side, _code in rows:
        counts[side] += 1
    assert counts == {"1": 1, "2": 1}  # one right, one left ulna fracture


def test_flagship_data_path_excludes1_conflicts(coding_session):
    """Excludes1 conflict detection across one patient's linked codes —
    deterministic, ready for the care-plan validator (design §8.2)."""
    edges_t = Base.metadata.tables["ontology_edges"]
    concepts_t = Base.metadata.tables["ontology_concepts"]
    # S52 (forearm fracture family) excludes1-references S58 (fixture note is
    # unresolvable) — so assert the query shape on excludes2 (S52 → S82):
    # a patient carrying codes under BOTH sides of the edge is flagged.
    patient = coding_session.query(Patient).filter_by(mrn="MRN20000001").one()
    visit = patient.visits[0]
    coding_session.add(
        Condition(patient=patient, visit=visit, icd10_code="S82.51XA", description="ankle too")
    )
    coding_session.commit()
    from hdh.modules.icd10cm.cli import _cmd_link

    _cmd_link(coding_session)

    dx_concepts = [
        row[0]
        for row in coding_session.execute(
            select(Condition.__table__.c.concept_id)
            .join(Visit, Condition.visit_id == Visit.id)
            .where(Visit.patient_id == patient.id, Condition.__table__.c.concept_id.isnot(None))
        )
    ]
    conflicts = coding_session.execute(
        select(edges_t.c.edge_type, edges_t.c.source_id, edges_t.c.target_id)
        .where(edges_t.c.edge_type.in_(("excludes1", "excludes2")))
        .join(concepts_t, edges_t.c.source_id == concepts_t.c.id)
    ).all()
    ancestors_by_concept = {
        cid: coding_session.execute(select(concepts_t.c.path).where(concepts_t.c.id == cid)).scalar()
        for cid in dx_concepts
    }

    def covers(rule_concept_id: str, dx_path: str) -> bool:
        rule_path = coding_session.execute(
            select(concepts_t.c.path).where(concepts_t.c.id == rule_concept_id)
        ).scalar()
        return dx_path == rule_path or dx_path.startswith(rule_path + ".")

    flagged = [
        (src, tgt)
        for _etype, src, tgt in conflicts
        if any(covers(src, p) for p in ancestors_by_concept.values())
        and any(covers(tgt, p) for p in ancestors_by_concept.values())
    ]
    assert flagged, "S52.001A + S82.51XA should trip the S52↔S82 rule edge"
