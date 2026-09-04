"""Reading a saved plan back, revising it, and superseding a decided one.

Measured before any of this existed: asked to show a saved care plan, the
agent had only `show_care_plan` — which reads the in-flight review
checkpoint — got "no care plan in progress for MRN57649249", and answered
**"No saved care plan exists"** about a patient whose plan #13 was sitting
in `care_plan_records`. The validator passed the turn, correctly by its own
rules: the answer was grounded in what the tool returned. The tool was
answering a different question.

That is the same failure the record tools hit once already — a capability
nobody can reach makes the model narrate instead of refuse — so the tests
here run in both directions: the behaviour works, *and* the tools that
perform it are reachable from the intent that needs them.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.modules.careplan.persist import (
    APPROVED,
    USER_EDITED,
    amend_plan,
    current_plan_id,
    decide,
    history,
    latest_plan_id,
    load_plan,
    persist_reviewed_plan,
    plans_for,
    render_record,
    superseded_by,
)


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=5, years_of_history=2, verbose=False, seed=11, as_of=date(2026, 8, 14))
    yield session, session.query(Patient).first()
    session.close()
    engine.dispose()


def _values(n: int = 3):
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft

    names = ["Polypharmacy", "Uncontrolled osteoarthritis", "Falls risk", "Frailty"][:n]
    return {
        "concerns": [
            ConcernDraft(f"{name} needs review", "risk", (f"src/{i}",)) for i, name in enumerate(names)
        ],
        "goals": [GoalDraft(f"Improve {name}", i, "", (f"src/{i}",)) for i, name in enumerate(names)],
        "interventions": [
            InterventionDraft(f"Act on {name}", i, "service", "GP", (f"src/{i}",))
            for i, name in enumerate(names)
        ],
        "deferred": ["Essential hypertension — controlled"],
    }


@pytest.fixture()
def saved(chart):
    session, patient = chart
    plan_id = persist_reviewed_plan(session, patient, _values()).plan_id
    return session, patient, plan_id


# ── reading a saved plan back ────────────────────────────────────────────


def test_a_saved_plan_can_be_read_back(saved):
    session, _patient, plan_id = saved
    plan = load_plan(session, plan_id)
    assert plan is not None
    assert len(plan["concerns"]) == 3
    assert len(plan["goals"]) == 3
    assert len(plan["interventions"]) == 3


def test_reading_a_plan_that_does_not_exist_returns_none(saved):
    session, _patient, _plan_id = saved
    assert load_plan(session, 9999) is None


def test_the_rendered_plan_shows_what_each_element_cites(saved):
    """The plan is graded on traceability; rendering it without citations
    would show a reviewer something better than what was written."""
    session, _patient, plan_id = saved
    text = render_record(load_plan(session, plan_id))
    assert "src/0" in text
    assert "Polypharmacy" in text


def test_the_rendered_plan_names_an_element_that_cites_nothing(saved):
    """Silence would read as a citation the reader cannot see."""
    from hdh.modules.careplan.generate import ConcernDraft

    session, patient, _plan_id = saved
    bare = {"concerns": [ConcernDraft("Uncited concern", "risk", ())], "goals": [], "interventions": []}
    plan_id = persist_reviewed_plan(session, patient, bare).plan_id
    assert "NOTHING" in render_record(load_plan(session, plan_id))


def test_what_triage_deferred_is_visible_in_the_record(saved):
    session, _patient, plan_id = saved
    assert "Essential hypertension" in render_record(load_plan(session, plan_id))


def test_a_patients_plans_are_listed_newest_first(chart):
    session, patient = chart
    first = persist_reviewed_plan(session, patient, _values()).plan_id
    second = persist_reviewed_plan(session, patient, _values()).plan_id
    assert [p["id"] for p in plans_for(session, patient.id)] == [second, first]


def test_a_patient_with_no_plans_lists_nothing(chart):
    session, patient = chart
    assert plans_for(session, patient.id) == []


# ── amending a plan nobody has decided ───────────────────────────────────


def test_an_undecided_plan_is_amended_in_place(saved):
    session, patient, plan_id = saved
    decision = amend_plan(session, plan_id, {1, 3}, "the second does not belong")
    assert decision and decision.plan_id == plan_id
    plan = load_plan(session, plan_id)
    assert [c.statement for c in plan["concerns"]] == ["Polypharmacy needs review", "Falls risk needs review"]
    assert latest_plan_id(session, patient.id) == plan_id, "no new plan should have been written"


def test_amending_takes_the_goals_and_interventions_with_it(saved):
    """A goal whose concern is gone is a goal nothing justifies."""
    session, _patient, plan_id = saved
    amend_plan(session, plan_id, {1})
    plan = load_plan(session, plan_id)
    assert len(plan["goals"]) == 1
    assert len(plan["interventions"]) == 1


def test_an_amendment_is_recorded(saved):
    session, _patient, plan_id = saved
    amend_plan(session, plan_id, {1, 2}, "falls risk is handled elsewhere")
    last = history(session, plan_id)[-1]
    assert last["action"] == "amend"
    assert last["before"]["concerns"] == 3
    assert last["after"]["concerns"] == 2
    assert "falls risk is handled elsewhere" in last["reason"]


def test_keeping_nothing_is_refused(saved):
    """Emptying a plan is a rejection, and a rejection has to say why."""
    session, _patient, plan_id = saved
    decision = amend_plan(session, plan_id, set())
    assert not decision and "reject the plan instead" in decision.detail


def test_keeping_a_concern_that_is_not_there_is_refused(saved):
    session, _patient, plan_id = saved
    decision = amend_plan(session, plan_id, {1, 9})
    assert not decision and "no 9" in decision.detail


def test_keeping_everything_is_not_an_amendment(saved):
    """It would write an audit event saying something changed."""
    session, _patient, plan_id = saved
    decision = amend_plan(session, plan_id, {1, 2, 3})
    assert not decision and "already has exactly those" in decision.detail


# ── amending a plan somebody decided ─────────────────────────────────────


def test_an_approved_plan_is_not_changed_by_an_amendment(saved):
    """The record of what was signed off has to survive its own revision."""
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "reviewed with the patient")
    amend_plan(session, plan_id, {1, 2}, "second thoughts on falls")
    original = load_plan(session, plan_id)
    assert len(original["concerns"]) == 3, "the approved plan was edited"
    assert original["row"].status == APPROVED


def test_amending_an_approved_plan_writes_a_successor(saved):
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    decision = amend_plan(session, plan_id, {1, 2}, "second thoughts")
    assert decision and decision.plan_id != plan_id
    successor = load_plan(session, decision.plan_id)
    assert len(successor["concerns"]) == 2
    assert successor["row"].supersedes_id == plan_id
    assert successor["row"].status == USER_EDITED, "a revision is not itself approved"


def test_superseding_does_not_un_approve_the_old_plan(saved):
    """It really was approved. Rewriting the status to say otherwise would
    destroy the fact this whole model exists to keep."""
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    amend_plan(session, plan_id, {1})
    assert load_plan(session, plan_id)["row"].status == APPROVED


def test_the_successor_is_findable_from_the_plan_it_replaced(saved):
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    new_id = amend_plan(session, plan_id, {1}).plan_id
    assert superseded_by(session, plan_id) == new_id
    assert superseded_by(session, new_id) is None


def test_current_means_nothing_superseded_it(saved):
    session, patient, plan_id = saved
    assert current_plan_id(session, patient.id) == plan_id
    decide(session, plan_id, True, "signed off")
    new_id = amend_plan(session, plan_id, {1}).plan_id
    assert current_plan_id(session, patient.id) == new_id
    listed = {p["id"]: p for p in plans_for(session, patient.id)}
    assert listed[plan_id]["current"] is False
    assert listed[new_id]["current"] is True


def test_both_plans_are_audited_when_one_supersedes_another(saved):
    """One event says a revision was written; the other says the approved
    plan was replaced. Without the second, the old plan's trail ends at its
    approval and gives no hint it is no longer the one in force."""
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    new_id = amend_plan(session, plan_id, {1}).plan_id
    assert [e["action"] for e in history(session, new_id)] == ["create"]
    old = history(session, plan_id)
    assert [e["action"] for e in old] == ["create", "approve", "amend"]
    assert old[-1]["after"]["superseded_by"] == new_id


def test_a_plan_is_superseded_once(saved):
    """A second successor would leave two plans claiming to replace it and
    no way to say which is in force."""
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    first = amend_plan(session, plan_id, {1, 2}).plan_id
    again = amend_plan(session, plan_id, {1})
    assert not again and f"already superseded by #{first}" in again.detail


def test_a_rejected_plan_is_also_superseded_rather_than_edited(saved):
    session, _patient, plan_id = saved
    decide(session, plan_id, False, "burden too high")
    decision = amend_plan(session, plan_id, {1})
    assert decision and decision.plan_id != plan_id


def test_the_successor_keeps_the_citations(saved):
    """A revision that dropped them would export claims without support."""
    session, _patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    new_id = amend_plan(session, plan_id, {1}).plan_id
    assert "src/0" in render_record(load_plan(session, new_id))


# ── the tools exist, and can actually be reached ─────────────────────────


def test_the_record_tools_are_exposed_for_the_care_plan_intent():
    """The reverse direction. A tool that exists but is not routable is how
    the agent came to describe a save it never performed, and how it came to
    deny a saved plan that was there."""
    from hdh.modules.agent.pipeline.gateway import INTENT_TOOLS

    assert {"get_care_plan", "list_care_plans", "amend_care_plan"} <= INTENT_TOOLS["care_plan"]


def test_every_careplan_tool_is_routable(db_session):
    """Pinning the whole set rather than the three added here: the failure
    repeats because each new tool is a fresh chance to forget."""
    pytest.importorskip("anthropic")
    from hdh.modules.agent.pipeline.gateway import INTENT_TOOLS
    from hdh.modules.careplan.agent_tools import build_careplan_tools

    built = {tool.name for tool in build_careplan_tools(db_session)}
    unroutable = built - INTENT_TOOLS["care_plan"]
    assert not unroutable, f"built but unreachable from the care_plan intent: {sorted(unroutable)}"


def test_reading_a_saved_plan_does_not_go_through_the_review_checkpoint(db_session):
    """The bug in one line. `show_care_plan` reads a review session;
    `get_care_plan` reads the record. Wiring the retrieval tool to the
    checkpoint would reproduce "No saved care plan exists" exactly."""
    pytest.importorskip("anthropic")
    import inspect

    from hdh.modules.careplan import agent_tools

    source = inspect.getsource(agent_tools._get_saved)
    assert "load_plan" in source
    assert "desk" not in source, "retrieval must not read the in-flight checkpoint"


# ── through the tool, not around it ──────────────────────────────────────
#
# Every test above calls `amend_plan` directly with the numbers a reader
# sees. The agent does not: it goes through `_amend_saved`, which parses a
# string with `_numbers` — and `_numbers` returns 0-BASED indices, because
# the review stages it was written for index into a list. So "1, 2" arrived
# as {0, 1}, and a real agent run came back "no 0" on a plan that was
# sitting right there. Passing tests, broken feature, one conversion apart.


def test_amending_through_the_tool_keeps_the_concerns_the_user_named(saved):
    from hdh.modules.careplan.agent_tools import _amend_saved

    session, patient, plan_id = saved
    out = _amend_saved(session, patient.mrn, "1, 3", 0, "the second is handled elsewhere")
    assert "no 0" not in out
    kept = [c.statement for c in load_plan(session, plan_id)["concerns"]]
    assert kept == ["Polypharmacy needs review", "Falls risk needs review"]


def test_the_tool_returns_the_amended_plan_so_the_agent_can_show_it(saved):
    from hdh.modules.careplan.agent_tools import _amend_saved

    session, patient, plan_id = saved
    out = _amend_saved(session, patient.mrn, "1", 0, "")
    assert f"care plan #{plan_id}" in out
    assert "Polypharmacy" in out


def test_amending_an_approved_plan_through_the_tool_supersedes_it(saved):
    from hdh.modules.careplan.agent_tools import _amend_saved

    session, patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    out = _amend_saved(session, patient.mrn, "1, 2", 0, "osteoarthritis managed elsewhere")
    assert "superseding it" in out
    assert len(load_plan(session, plan_id)["concerns"]) == 3, "the approved plan was edited"


def test_a_concern_number_that_does_not_exist_is_refused_in_words(saved):
    """The reviewer meant something by typing 9. Clamping to what exists
    would look like it worked."""
    from hdh.modules.careplan.agent_tools import _amend_saved

    session, patient, _plan_id = saved
    out = _amend_saved(session, patient.mrn, "1, 9", 0, "")
    assert "no item 9" in out
    assert "get_care_plan" in out


def test_unparseable_input_is_refused_rather_than_raised(saved):
    """A ValueError out of a tool is a stack trace in the transcript, not an
    answer the model can act on."""
    from hdh.modules.careplan.agent_tools import _amend_saved

    session, patient, _plan_id = saved
    out = _amend_saved(session, patient.mrn, "the first two", 0, "")
    assert "not an item number" in out


def test_retrieving_through_the_tool_finds_the_saved_plan(saved):
    """The original bug, at the seam it actually failed on."""
    from hdh.modules.careplan.agent_tools import _get_saved

    session, patient, plan_id = saved
    out = _get_saved(session, patient.mrn, 0)
    assert f"care plan #{plan_id}" in out
    assert "no care plan in progress" not in out


def test_listing_through_the_tool_says_which_plan_is_current(saved):
    from hdh.modules.careplan.agent_tools import _list_saved

    session, patient, plan_id = saved
    decide(session, plan_id, True, "signed off")
    new_id = amend_plan(session, plan_id, {1}).plan_id
    out = _list_saved(session, patient.mrn)
    assert f"#{new_id}" in out and "current" in out
    assert f"superseded by #{new_id}" in out


def test_a_patient_with_no_plans_is_told_so_not_shown_an_empty_plan(chart):
    from hdh.modules.careplan.agent_tools import _get_saved, _list_saved

    session, patient = chart
    assert "no saved care plan" in _get_saved(session, patient.mrn, 0)
    assert "no saved care plan" in _list_saved(session, patient.mrn)


# ── exactly one plan is in force ─────────────────────────────────────────


def test_only_one_plan_is_ever_current(chart):
    """Two plans labelled current is not a display detail. Plans are saved
    independently and only an amendment links them, so "has no successor"
    marked several current at once — and a clinician reading that cannot
    tell which plan to act on."""
    session, patient = chart
    older = persist_reviewed_plan(session, patient, _values()).plan_id
    newer = persist_reviewed_plan(session, patient, _values()).plan_id
    listed = {p["id"]: p for p in plans_for(session, patient.id)}
    assert listed[newer]["current"] is True
    assert listed[older]["current"] is False, "an older independent plan is not in force"
    assert current_plan_id(session, patient.id) == newer


def test_an_older_plan_reads_as_not_in_force_rather_than_superseded(chart):
    """It was not replaced — nothing points at it. Saying "superseded" would
    invent a link the record does not have."""
    session, patient = chart
    older = persist_reviewed_plan(session, patient, _values()).plan_id
    persist_reviewed_plan(session, patient, _values())
    assert "not in force" in render_record(load_plan(session, older))
    assert "superseded" not in render_record(load_plan(session, older))


def test_the_plan_in_force_says_so(chart):
    session, patient = chart
    persist_reviewed_plan(session, patient, _values())
    newest = persist_reviewed_plan(session, patient, _values()).plan_id
    assert "(current)" in render_record(load_plan(session, newest))
