"""S4c: the plan as a clinician reads it.

The page has one job the terminal rendering cannot do — make a question the
scores cannot answer answerable by looking. Traceability governs 21 of 24
verdicts on the cohort, so an element citing nothing has to be the loudest
thing on the page, not a quiet gap.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hdh.modules.careplan.render import (
    NO_CITATION,
    Framing,
    PlanView,
    plan_html,
    uncited,
    view_from_state,
    write_plan_html,
)


@dataclass(frozen=True)
class _Item:
    statement: str
    evidence_refs: tuple[str, ...] = ()
    concern_index: int = 0
    goal_index: int = 0
    concern_type: str = ""
    target_value: str = ""
    owner_role: str = ""


def _view(**overrides) -> PlanView:
    defaults = dict(
        mrn="MRN0001",
        age=83,
        sex="FEMALE",
        concerns=[_Item("Heart failure, poorly controlled", ("hf/doc#2",), concern_type="condition")],
        goals=[_Item("Reduce admissions", ("hf/doc#4",), target_value="0 in 12 months")],
        interventions=[_Item("Daily weights", ("hf/doc#5",), owner_role="GP")],
    )
    defaults.update(overrides)
    return PlanView(**defaults)


# ── the property the page exists for ─────────────────────────────────────


def test_an_element_citing_nothing_is_marked(_=None):
    """Not omitted, not blank — named. A missing citation is the single most
    important thing on a traceability-governed plan."""
    html = plan_html(_view(goals=[_Item("Reduce admissions")]))
    assert NO_CITATION in html
    assert 'class="ref none"' in html


def test_an_element_that_cites_shows_what_it_cites():
    html = plan_html(_view())
    assert "hf/doc#2" in html and "hf/doc#5" in html


def test_uncited_can_be_asked_without_parsing_html():
    """The count is the number the plan is graded on. A caller should not
    have to read the page to learn it."""
    view = _view(
        concerns=[_Item("A", ("x",))],
        goals=[_Item("B")],
        interventions=[_Item("C"), _Item("D", ("y",))],
    )
    missing = uncited(view)
    assert missing == ["goal: B", "intervention: C"]


# ── the clinical shape survives ──────────────────────────────────────────


def test_interventions_sit_under_the_goal_they_serve():
    """A flat list would lose the argument the plan is making."""
    view = _view(
        concerns=[_Item("Concern one"), _Item("Concern two")],
        goals=[_Item("Goal for one", concern_index=0), _Item("Goal for two", concern_index=1)],
        interventions=[_Item("Action for goal two", goal_index=1)],
    )
    html = plan_html(view)
    after_goal_two = html.split("Goal for two", 1)[1]
    assert "Action for goal two" in after_goal_two
    assert "Action for goal two" not in html.split("Goal for two", 1)[0]


def test_a_goal_with_no_interventions_says_so():
    view = _view(interventions=[])
    assert "No interventions were proposed" in plan_html(view)


def test_a_concern_with_no_goals_says_so():
    view = _view(goals=[], interventions=[])
    assert "No goals were set" in plan_html(view)


# ── what was withheld, split by cause ────────────────────────────────────


def test_deferred_and_declined_are_not_merged():
    """Different absences: triage removed a problem before the model saw it;
    the model refused a candidate it was offered. A reviewer told only that
    something is missing cannot tell which happened."""
    view = _view(
        deferred=["Vitamin D deficiency"],
        dropped={"dropped_concerns": ["Hyperlipidaemia — cited nothing offered"]},
    )
    html = plan_html(view)
    assert "never offered to the model" in html
    assert "offered and refused" in html
    assert "Vitamin D deficiency" in html
    assert "Hyperlipidaemia" in html


def test_nothing_withheld_is_stated_rather_than_left_blank():
    assert "Nothing was deferred or declined" in plan_html(_view())


# ── the grade says what decided it ───────────────────────────────────────


def test_the_governing_dimension_is_marked_not_just_listed():
    """The lowest dimension decides and the mean is only reported. A table
    showing an average without saying which row produced the verdict would
    reproduce the misreading the rubric exists to prevent."""
    view = _view(
        scores={"completeness": 5, "traceability": 2, "safety": 4},
        verdict="fail",
    )
    html = plan_html(view)
    assert "governs the verdict" in html
    assert 'class="governs"' in html
    assert "The lowest dimension decides" in html


def test_the_mean_is_reported_but_not_presented_as_the_verdict():
    view = _view(scores={"a": 5, "b": 1}, verdict="fail")
    html = plan_html(view)
    assert "Mean 3.0" in html
    assert "reported only" in html


def test_an_ungraded_plan_shows_no_grade_section():
    assert "Grade" not in plan_html(_view())


# ── it is synthetic, and cannot stop saying so ───────────────────────────


def test_the_page_declares_itself_synthetic():
    """Nothing hdh renders should be mistakable for a real patient record."""
    html = plan_html(_view())
    assert "Synthetic record" in html
    assert "Not a real patient" in html


def test_the_page_is_self_contained():
    """A published page blocks external requests, so a linked font or script
    would not degrade — it would silently vanish."""
    html = plan_html(_view())
    for forbidden in ("http://", "https://", "<script", "@import"):
        assert forbidden not in html, f"page reaches outside itself: {forbidden}"


def test_the_page_styles_both_themes_through_tokens():
    html = plan_html(_view())
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    assert "background: var(--ground)" in html


def test_patient_free_text_cannot_inject_markup():
    view = _view(concerns=[_Item("<script>alert(1)</script>", ("x",))])
    html = plan_html(view)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── building the view from real plan state ───────────────────────────────


def test_a_view_reads_the_channels_plan_state_actually_uses():
    values = {
        "concerns": [_Item("C", ("a",))],
        "goals": [_Item("G")],
        "interventions": [_Item("I")],
        "deferred": ["something"],
        "dropped_goals": ["a rejected goal"],
    }
    view = view_from_state("MRN9", values, Framing(age=70, sex="MALE"))
    assert view.age == 70
    assert view.deferred == ["something"]
    assert view.dropped == {"dropped_goals": ["a rejected goal"]}


def test_a_view_falls_back_to_raw_interventions_before_reconcile_runs():
    """A plan paused mid-run has raw_interventions and no reconciled list;
    showing nothing there would look like the stage produced nothing."""
    values = {"raw_interventions": [_Item("I")]}
    assert len(view_from_state("MRN9", values).interventions) == 1


def test_writing_the_page_returns_where_it_went(tmp_path):
    target = tmp_path / "nested" / "plan.html"
    written = write_plan_html(target, _view())
    assert written == str(target)
    assert target.read_text(encoding="utf-8").startswith("<title>")


@pytest.mark.parametrize("field", ["concerns", "goals", "interventions"])
def test_an_empty_plan_renders_rather_than_raising(field):
    view = _view(**{field: []})
    assert plan_html(view)
