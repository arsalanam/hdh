"""S4b: care planning through the agent, and the two things it must not do.

The tools are thin by design — each is a call into `review` plus a
rendering — so these tests spend most of their attention on the guarantees
rather than on the plumbing: the pack writes no chart rows, and it cannot
edit the standard it is graded against.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan.agent_tools import build_careplan_tools


def _names(tools) -> set[str]:
    """Tool names as the API sees them.

    `beta_tool` returns a BetaFunctionTool carrying `.name`; `__name__` on
    one is always None. Reading the wrong attribute yields an empty set, and
    every "no tool does X" assertion then passes without checking anything —
    which is exactly what happened to the guard in #121.
    """
    names = {tool.name for tool in tools}
    assert names and all(names), "no tool names — an absence test here would be vacuous"
    return names


def _tool(tools, name: str):
    return next(tool for tool in tools if tool.name == name)


@pytest.fixture()
def tools():
    return build_careplan_tools(session=None)


# ── the guarantees ───────────────────────────────────────────────────────


def test_the_pack_creates_no_chart_rows(tools):
    """#121's property, extended to the newest surface.

    A fact enters the chart only as the outcome of a fulfilment. A care plan
    is a plan *about* a chart, not a fact in one — so nothing here may make
    a condition, a prescription, a lab or a dispense.
    """
    forbidden = ("condition", "prescription", "lab", "dispense", "visit", "allerg")
    offenders = {
        name
        for name in _names(tools)
        if any(word in name for word in forbidden) and ("add" in name or "create" in name)
    }
    assert not offenders, f"the care-plan pack gained a chart-writing tool: {offenders}"


def test_there_is_no_way_to_edit_a_rubric(tools):
    """A system able to rewrite the standard it is graded against has no
    grade. Rubrics change by editing files, reviewed like any other change,
    and then measured on the cohort."""
    writers = {name for name in _names(tools) if "rubric" in name and not name.startswith("show_")}
    assert not writers, f"a rubric-writing tool appeared: {writers}"


def test_every_review_verb_is_offered(tools):
    """Approve, amend and reject. Offering only approve would make the pause
    decorative — a reviewer who cannot send a stage back will accept it."""
    names = _names(tools)
    assert {"approve_care_plan_stage", "amend_care_plan_stage", "reject_care_plan_stage"} <= names


def test_the_pack_is_registered_with_the_agent():
    """An unregistered pack is a pack nobody can call."""
    from hdh.modules.agent.tools import _ONTOLOGY_BUILDERS

    assert ("hdh.modules.careplan.agent_tools", "build_careplan_tools") in _ONTOLOGY_BUILDERS


# ── item selection refuses what it cannot honour ─────────────────────────


def test_amending_to_a_number_that_does_not_exist_refuses():
    """Clamping would look like it worked. A reviewer who typed 7 when there
    are 5 items meant something, and keeping the first five is not it."""
    from hdh.modules.careplan.agent_tools import _numbers

    with pytest.raises(ValueError, match="no item 7"):
        _numbers("1,7", limit=5)


def test_amending_with_words_refuses():
    from hdh.modules.careplan.agent_tools import _numbers

    with pytest.raises(ValueError, match="not an item number"):
        _numbers("the first one", limit=5)


def test_keeping_none_is_a_valid_amendment():
    """Dropping every proposed item is a real clinical answer — the stage
    found nothing worth carrying — and must not be confused with an error."""
    from hdh.modules.careplan.agent_tools import _numbers

    assert _numbers("", limit=5) == []


def test_item_numbers_are_the_ones_the_clinician_was_shown():
    """1-based, because that is what the rendering prints."""
    from hdh.modules.careplan.agent_tools import _numbers

    assert _numbers("1,3", limit=3) == [0, 2]


# ── the rendering tells the reviewer what they may do ────────────────────


def test_the_rendering_names_the_next_actions():
    """A reviewer not told that rejecting is available will approve things
    they would have sent back."""
    from hdh.modules.careplan.agent_tools import _render
    from hdh.modules.careplan.review import Pause

    pause = Pause(node="concerns", next_node="goals", values={"concerns": []})
    text = _render("MRN01", pause)
    assert "Approve" in text and "amend" in text and "reject" in text
    assert "goals" in text


def test_a_finished_plan_does_not_offer_a_stage_to_review():
    from hdh.modules.careplan.agent_tools import _render
    from hdh.modules.careplan.review import Pause

    pause = Pause(node="interventions", next_node=None, values={"interventions": []})
    text = _render("MRN01", pause)
    assert "Nothing left to review" in text
    assert "Approve to run" not in text


def test_one_thread_per_patient_so_a_plan_can_be_found_again():
    from hdh.modules.careplan.agent_tools import _thread_for

    assert _thread_for("mrn01 ") == _thread_for("MRN01") != _thread_for("MRN02")


# ── the rubric is readable, and says what governs a verdict ──────────────


def test_the_rubric_tool_explains_that_the_lowest_dimension_governs(tools):
    """The single most load-bearing fact about the scoring, and the one a
    reader most often assumes otherwise: the mean is reported, not applied."""
    show = _tool(tools, "show_care_plan_rubric")
    text = show(name="")
    assert "LOWEST dimension governs" in text
    assert "traceability" in text


def test_an_unknown_rubric_names_the_ones_that_exist(tools):
    show = _tool(tools, "show_care_plan_rubric")
    text = show(name="not-a-rubric")
    assert "default" in text and "multimorbid-elderly" in text


# ── the whole loop, driven the way the agent drives it ───────────────────


@pytest.fixture()
def conversation(tmp_path):
    """One patient, a fake corpus and selector, and the tools over them.

    Injected rather than real, so this exercises the pack's own behaviour
    instead of PostgreSQL retrieval and an API key.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.graph import PlanServices, compile_pipeline
    from hdh.modules.careplan.knowledge import KnowledgeHit

    class FakeStore:
        def search(self, query, corpus, k=5, filters=None):
            return [
                KnowledgeHit(
                    corpus="med_safety",
                    doc_id=f"doc{n}",
                    chunk=f"Guidance {n}.",
                    score=0.9 - n / 10,
                    source="notes",
                    license="MIT",
                    metadata={},
                )
                for n in range(3)
            ][:k]

    def selector(task):
        properties = task.schema["properties"]["selections"]["items"]["properties"]
        items = []
        for n in range(2):
            item = {"statement": f"Statement {n}", "cites": [f"med_safety/doc{n}"]}
            if "concern_type" in properties:
                item["concern_type"] = "condition"
            if "concern_index" in properties:
                item["concern_index"] = 0
                item["target_value"] = ""
            if "goal_index" in properties:
                item["goal_index"] = 0
                item["intervention_type"] = "monitoring"
                item["owner_role"] = "GP"
            items.append(item)
        return {"selections": items}

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=6, years_of_history=3, verbose=False, seed=7)
    mrn = session.query(Patient).first().mrn

    services = PlanServices(store=FakeStore(), selector=selector)
    graph = compile_pipeline(checkpointer=InMemorySaver(), review=True)
    tools = build_careplan_tools(session, services=services, graph=graph)
    yield tools, mrn
    session.close()
    engine.dispose()


def test_a_plan_can_be_built_by_talking_to_it(conversation):
    """Start, amend, approve, finish — the loop S4b exists to provide."""
    tools, mrn = conversation
    start = _tool(tools, "start_care_plan")
    amend = _tool(tools, "amend_care_plan_stage")
    approve = _tool(tools, "approve_care_plan_stage")

    opened = start(mrn=mrn)
    assert "paused after concerns" in opened
    assert "Statement 0" in opened

    # How many concerns come back depends on how many topics triage found
    # for this patient, so the count is read rather than assumed — only the
    # amendment itself is asserted.
    kept = amend(mrn=mrn, keep="1")
    assert kept.startswith("kept 1 of ")
    assert "concerns" in kept.splitlines()[0]
    assert "paused after goals" in kept

    assert "paused after interventions" in approve(mrn=mrn)
    assert "Nothing left to review" in approve(mrn=mrn)


def test_starting_twice_does_not_silently_discard_the_first(conversation):
    """A plan half-reviewed is work the clinician did. Overwriting it
    because they said "start" again would throw that away without asking."""
    tools, mrn = conversation
    start = _tool(tools, "start_care_plan")
    start(mrn=mrn)
    again = start(mrn=mrn)
    assert "already open" in again and "restart=true" in again


def test_restarting_is_available_when_asked_for(conversation):
    tools, mrn = conversation
    start = _tool(tools, "start_care_plan")
    start(mrn=mrn)
    _tool(tools, "approve_care_plan_stage")(mrn=mrn)
    assert "paused after concerns" in start(mrn=mrn, restart=True)


def test_a_rejected_stage_comes_back_for_review(conversation):
    tools, mrn = conversation
    _tool(tools, "start_care_plan")(mrn=mrn)
    text = _tool(tools, "reject_care_plan_stage")(mrn=mrn, feedback="none of these are urgent")
    assert "paused after concerns" in text


def test_rejecting_without_a_reason_is_refused_in_words(conversation):
    """The tool returns the refusal rather than raising: the agent has to be
    able to tell the user why, not crash the turn."""
    tools, mrn = conversation
    _tool(tools, "start_care_plan")(mrn=mrn)
    assert "needs a reason" in _tool(tools, "reject_care_plan_stage")(mrn=mrn, feedback="")


def test_an_unknown_patient_is_said_plainly(conversation):
    tools, _mrn = conversation
    assert "no patient" in _tool(tools, "start_care_plan")(mrn="MRN-NOPE")


def test_showing_a_plan_nobody_started_says_so(conversation):
    tools, _mrn = conversation
    assert "no care plan in progress" in _tool(tools, "show_care_plan")(mrn="MRN00000001")
