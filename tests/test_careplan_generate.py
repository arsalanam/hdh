"""Care plan, milestone 2b: constrained generation.

Design §7 nodes 3-5 and 7, plus the structural validation after them.

Everything here runs with a **fake store and a stub selector** — no
PostgreSQL, no API key — which is the arrangement `test_pipeline.py`
already uses for the agent graph. That is not only a CI convenience: a
graph that can only be exercised by paying for tokens is a graph nobody
exercises.

The rule these tests exist to hold is the one that separates this from a
chatbot with a database: **the model selects and phrases; it never invents
a clinical claim.** A selection citing something that was not offered is
dropped.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.models import Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.careplan.context import CarePlanContext, MedicationView, SocialView
from hdh.modules.careplan.generate import (
    PlanDraft,
    propose_concerns,
    propose_goals,
    propose_interventions,
    stub_selector,
)
from hdh.modules.careplan.knowledge import KnowledgeHit


class FakeStore:
    """Returns fixed hits, and records what it was asked."""

    def __init__(self, hits: list[KnowledgeHit] | None = None) -> None:
        self._hits = hits if hits is not None else [_hit("sulfonylurea-older-adults")]
        self.queries: list[str] = []

    def search(self, query, corpus, k=5, filters=None):
        self.queries.append(query)
        return list(self._hits)[:k]


def _hit(doc_id: str, corpus: str = "med_safety") -> KnowledgeHit:
    return KnowledgeHit(
        corpus=corpus,
        doc_id=doc_id,
        chunk=f"Text of {doc_id}.",
        score=0.44,
        source="hdh med-safety notes",
        license="MIT",
        metadata={},
    )


def _context() -> CarePlanContext:
    return CarePlanContext(
        mrn="MRN-GEN",
        age=84,
        sex="MALE",
        as_of=date(2026, 8, 1),
        medications=(MedicationView("Glipizide", "Sulfonylurea", "5 mg", date(2026, 5, 1)),),
        social=SocialView(
            lives_alone=True, lives_alone_basis="none recorded", smoker=None, marital_status=None
        ),
    )


# ── the rule: cite what was offered, or be dropped ───────────────────────


def test_a_concern_citing_an_offered_chunk_is_kept():
    store = FakeStore()
    selector = stub_selector(
        [
            {
                "selections": [
                    {
                        "statement": "Risk of severe hypoglycaemia",
                        "concern_type": "risk",
                        "cites": ["med_safety/sulfonylurea-older-adults"],
                    }
                ]
            }
        ]
    )
    concerns, dropped = propose_concerns(store, _context(), (), selector)
    assert len(concerns) == 1
    assert concerns[0].evidence_refs == ("med_safety/sulfonylurea-older-adults",)
    assert not dropped


def test_a_concern_citing_something_never_offered_is_dropped():
    """The whole point. A fluent, plausible, entirely unsupported claim —
    which is exactly what a model produces when it is confident and wrong."""
    store = FakeStore()
    selector = stub_selector(
        [
            {
                "selections": [
                    {
                        "statement": "Patient requires immediate dialysis",
                        "concern_type": "condition",
                        "cites": ["med_safety/a-document-that-does-not-exist"],
                    }
                ]
            }
        ]
    )
    concerns, dropped = propose_concerns(store, _context(), (), selector)
    assert concerns == []
    assert dropped and "cited nothing that was offered" in dropped[0]


def test_a_concern_citing_nothing_at_all_is_dropped():
    store = FakeStore()
    selector = stub_selector(
        [{"selections": [{"statement": "Something worrying", "concern_type": "risk", "cites": []}]}]
    )
    concerns, dropped = propose_concerns(store, _context(), (), selector)
    assert concerns == []
    assert dropped


def test_dropping_is_recorded_rather_than_silent():
    """Silently discarding it would hide a model doing the one thing this
    design forbids — the drop has to be visible to whoever reviews."""
    store = FakeStore()
    selector = stub_selector(
        [{"selections": [{"statement": "Invented claim", "concern_type": "risk", "cites": ["nope"]}]}]
    )
    _concerns, dropped = propose_concerns(store, _context(), (), selector)
    assert any("Invented claim" in d for d in dropped)


def test_no_retrieval_means_no_concern():
    """§7: it may not emit a concern with no retrieval support. With an
    empty store there is nothing to support anything, and the node says so
    instead of falling back on the model's own knowledge."""
    store = FakeStore(hits=[])
    concerns, dropped = propose_concerns(store, _context(), (), stub_selector([]))
    assert concerns == []
    assert dropped and "no knowledge retrieved" in dropped[0]


# ── the graph keeps its shape ────────────────────────────────────────────


def test_each_goal_is_generated_against_one_concern():
    """Node 4 runs per concern, so a goal cannot outlive its reason — the
    index it carries is assigned by the loop, not chosen by the model."""
    store = FakeStore()
    concerns, _ = propose_concerns(
        store,
        _context(),
        (),
        stub_selector(
            [
                {
                    "selections": [
                        {
                            "statement": "Concern A",
                            "concern_type": "risk",
                            "cites": ["med_safety/sulfonylurea-older-adults"],
                        },
                        {
                            "statement": "Concern B",
                            "concern_type": "sdoh",
                            "cites": ["med_safety/sulfonylurea-older-adults"],
                        },
                    ]
                }
            ]
        ),
    )
    assert len(concerns) == 2

    goal_answer = {
        "selections": [
            {
                "statement": "A goal",
                "concern_index": 99,  # the model's number is ignored
                "cites": ["med_safety/sulfonylurea-older-adults"],
            }
        ]
    }
    goals, _ = propose_goals(store, _context(), concerns, stub_selector([goal_answer, goal_answer]))
    assert [g.concern_index for g in goals] == [0, 1], "goal bound to the concern it was generated for"


def test_interventions_bind_to_their_goal_the_same_way():
    store = FakeStore()
    goals = [
        type("G", (), {"statement": "Goal one"})(),
        type("G", (), {"statement": "Goal two"})(),
    ]
    answer = {
        "selections": [
            {
                "statement": "Do a thing",
                "goal_index": 42,
                "intervention_type": "monitoring",
                "owner_role": "nurse",
                "cites": ["med_safety/sulfonylurea-older-adults"],
            }
        ]
    }
    interventions, _ = propose_interventions(store, _context(), goals, stub_selector([answer, answer]))
    assert [i.goal_index for i in interventions] == [0, 1]


def test_retrieval_is_scoped_to_the_thing_being_answered():
    """Node 4 retrieves per concern rather than once for the patient —
    otherwise every goal is chosen from the same menu and the plan says the
    same thing four times."""
    store = FakeStore()
    concerns = [
        type("C", (), {"statement": "Hypoglycaemia risk"})(),
        type("C", (), {"statement": "Social isolation"})(),
    ]
    empty = {"selections": []}
    propose_goals(store, _context(), concerns, stub_selector([empty, empty]))
    assert "Hypoglycaemia risk" in store.queries[-2]
    assert "Social isolation" in store.queries[-1]


# ── assemble and validate ────────────────────────────────────────────────


@pytest.fixture
def chart(tmp_path):
    from hdh.core.models import Base

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "plan.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    patient = Patient(
        mrn="MRN-GEN",
        first_name="Gen",
        last_name="Erated",
        date_of_birth=date(1942, 1, 1),
        sex=Sex.MALE,
    )
    session.add(patient)
    session.flush()
    yield session, patient
    session.close()
    engine.dispose()


def _full_draft() -> PlanDraft:
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft

    ref = ("med_safety/sulfonylurea-older-adults",)
    draft = PlanDraft()
    draft.concerns.append(ConcernDraft("Risk of severe hypoglycaemia", "risk", ref))
    draft.goals.append(GoalDraft("No severe hypoglycaemic event", 0, "zero events", ref))
    draft.interventions.append(
        InterventionDraft("Review the sulfonylurea", 0, "medication", "prescriber", ref)
    )
    return draft


def test_a_written_plan_validates_and_traces_upward(chart):
    from hdh.modules.careplan.assemble import assemble, validate

    session, patient = chart
    plan_id = assemble(session, patient, _full_draft(), "A plan")
    report = validate(session, plan_id)
    assert report.ok, report.errors
    assert any("1 concern(s)" in c for c in report.checked)


def test_validation_rejects_an_ai_element_with_no_evidence(chart):
    """Redundant with the node-level drop, and deliberately so: this is the
    last gate before a plan exists, and the guarantee is worth two checks."""
    from sqlalchemy import insert

    from hdh.core.models import Base
    from hdh.modules.careplan.assemble import assemble, validate

    session, patient = chart
    plan_id = assemble(session, patient, _full_draft(), "A plan")
    session.execute(
        insert(Base.metadata.tables["health_concerns"]),
        [
            {
                "care_plan_id": plan_id,
                "concern_type": "risk",
                "statement": "Snuck in with no evidence",
                "source": "ai",
                "evidence_refs": {},
            }
        ],
    )
    session.flush()
    report = validate(session, plan_id)
    assert not report.ok
    assert any("no evidence" in e for e in report.errors)


def test_a_human_authored_element_needs_no_evidence(chart):
    """The rule is about what the *model* proposed. A clinician adding a
    goal is the authority, and demanding a citation from them would be
    applying the wrong standard to the right person."""
    from sqlalchemy import insert

    from hdh.core.models import Base
    from hdh.modules.careplan.assemble import assemble, validate

    session, patient = chart
    plan_id = assemble(session, patient, _full_draft(), "A plan")
    session.execute(
        insert(Base.metadata.tables["health_concerns"]),
        [
            {
                "care_plan_id": plan_id,
                "concern_type": "functional",
                "statement": "Wants to keep gardening",
                "source": "human",
                "evidence_refs": {},
            }
        ],
    )
    session.flush()
    assert validate(session, plan_id).ok


def test_an_empty_plan_is_not_written_at_all(chart):
    """An empty plan recorded as a plan reports that the patient was
    assessed and nothing found — when what happened is that nothing could
    be supported. Those are different, and the chart should say which."""
    from hdh.modules.careplan.plan import generate_plan

    session, patient = chart
    result = generate_plan(session, patient, store=FakeStore(hits=[]), selector=stub_selector([]))
    assert result.refused
    assert result.plan_id is None
    assert any("no plan written" in e for e in result.report.errors)


def test_the_whole_graph_runs_end_to_end_with_no_llm(chart):
    """§13: the full graph in pytest, no API key. A graph that can only be
    exercised by paying for tokens is a graph nobody exercises."""
    from hdh.modules.careplan.plan import generate_plan

    session, patient = chart
    ref = ["med_safety/sulfonylurea-older-adults"]
    result = generate_plan(
        session,
        patient,
        store=FakeStore(),
        selector=stub_selector(
            [
                {"selections": [{"statement": "Hypoglycaemia risk", "concern_type": "risk", "cites": ref}]},
                {"selections": [{"statement": "No severe event", "concern_index": 0, "cites": ref}]},
                {
                    "selections": [
                        {
                            "statement": "Review the sulfonylurea",
                            "goal_index": 0,
                            "intervention_type": "medication",
                            "owner_role": "prescriber",
                            "cites": ref,
                        }
                    ]
                },
            ]
        ),
    )
    assert not result.refused, result.report.errors
    assert result.report.ok
    assert len(result.draft.concerns) == 1
    assert len(result.draft.goals) == 1
    assert len(result.draft.interventions) == 1


def test_a_generated_plan_is_never_written_as_approved(chart):
    """Status is the safety story: nothing downstream may mistake a
    proposal for a decision somebody made."""
    from sqlalchemy import select

    from hdh.core.models import Base
    from hdh.modules.careplan.assemble import assemble

    session, patient = chart
    plan_id = assemble(session, patient, _full_draft(), "A plan")
    plans = Base.metadata.tables["care_plan_records"]
    status = session.execute(select(plans.c.status).where(plans.c.id == plan_id)).scalar()
    assert status == "ai_generated"
