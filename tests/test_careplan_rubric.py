"""Care plan, milestone 3a: the rubric, the facts, and the arithmetic.

Design §9. Everything here runs with no database and no API key — the
grader is injected, exactly as the selector is in `test_careplan_generate`.

Two of these tests are the reason the milestone is shaped this way. The
verdict test pins the decision that a mean must never rescue a bad safety
score, and the ungraded tests pin the decision that a score nobody could
compute is not a pass. Both are the kind of thing that looks like a detail
until the day a plan with one dangerous line scores 4.3 and reads as fine.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest

from hdh.modules.careplan.context import CarePlanContext, MedicationView, ProblemView
from hdh.modules.careplan.evaluate import FAIL, PASS, REVISE, Evaluation, evaluate, stub_grader
from hdh.modules.careplan.facts import NOT_RECORDED, PlanEvidence, compute_facts, mentions, render
from hdh.modules.careplan.reconcile import ReconcileReport
from hdh.modules.careplan.rubric import BUNDLED, RubricError, load_rubrics, parse_rubric, select_rubric
from hdh.modules.careplan.stratify import RiskFlag

# ── fixtures: charts, and rows that look enough like the real ones ───────


def _context(age: int = 78, problems=(), medications=()) -> CarePlanContext:
    return CarePlanContext(
        mrn="TEST01",
        age=age,
        sex="MALE",
        problems=tuple(ProblemView(code, text, controlled, None) for code, text, controlled in problems),
        medications=tuple(MedicationView(name, klass, "", None) for name, klass in medications),
    )


ELDERLY = _context(
    age=78,
    problems=[("E11.9", "Type 2 diabetes mellitus", False), ("N18.4", "Chronic kidney disease", True)],
    medications=[
        ("Glipizide", "Sulfonylurea"),
        ("Metformin", "Biguanide"),
        ("Ramipril", "ACE inhibitor"),
        ("Atorvastatin", "Statin"),
        ("Aspirin", "Antiplatelet"),
    ],
)


@dataclass
class Row:
    """Enough of a SQLAlchemy row for the facts to read."""

    id: int
    statement: str = ""
    source: str = "ai"
    evidence_refs: dict | None = None
    concern_id: int | None = None
    goal_id: int | None = None
    concern_type: str = "risk"
    intervention_type: str = "monitoring"
    owner_role: str = ""
    target_value: str = ""


def _plan(concerns=(), goals=(), interventions=(), context=ELDERLY, flags=(), reconciliation=None):
    return PlanEvidence(
        context=context,
        flags=tuple(flags),
        concerns=tuple(concerns),
        goals=tuple(goals),
        interventions=tuple(interventions),
        reconciliation=reconciliation,
    )


def _cited(**kwargs) -> Row:
    kwargs.setdefault("evidence_refs", {"chunks": ["med_safety/x#0"]})
    return Row(**kwargs)


# ── the rubrics that ship ────────────────────────────────────────────────


def test_every_bundled_rubric_loads():
    rubrics = load_rubrics()
    assert {rubric.rubric_id for rubric in rubrics} >= {"default", "multimorbid-elderly"}


def test_every_dimension_cites_a_source():
    """The same rule the corpus enforces on documents. A rubric asserts
    what good care looks like; one that cannot say where that standard came
    from is an opinion wearing a score."""
    for rubric in load_rubrics():
        for dimension in rubric.dimensions:
            assert dimension.source.strip(), f"{rubric.rubric_id}/{dimension.id}"


def test_a_fallback_rubric_exists():
    """Selection must never fail on a patient who simply isn't remarkable.
    An empty match block is what makes that true without special-casing."""
    assert any(not rubric.match for rubric in load_rubrics())


def _raw(name: str = "default.json") -> dict:
    return json.loads((BUNDLED / name).read_text(encoding="utf-8"))


def test_a_dimension_must_anchor_both_ends_of_its_scale():
    """A scale whose top is undefined asks the grader to invent what a 5
    means — and anchoring exists precisely so it does not have to."""
    raw = _raw()
    raw["dimensions"][0]["anchors"].pop("5")
    with pytest.raises(RubricError, match="does not anchor level 5"):
        parse_rubric(raw)


def test_a_dimension_may_anchor_sparsely_between_the_ends():
    """1/3/5 is a legitimate rubric. Only the endpoints are compulsory."""
    raw = _raw()
    raw["dimensions"][0]["anchors"] = {"1": "worst", "5": "best"}
    assert parse_rubric(raw).dimensions[0].anchors == {1: "worst", 5: "best"}


def test_a_dimension_asking_for_an_unknown_fact_does_not_load():
    """A typo here would produce an empty fact block at grading time, and
    the grader would silently work the answer out for itself — which is
    the one thing §9 says to stop it doing."""
    raw = _raw()
    raw["dimensions"][0]["facts"] = ["problem_kount"]
    with pytest.raises(RubricError, match="unknown fact"):
        parse_rubric(raw)


def test_a_dimension_with_an_empty_source_does_not_load():
    raw = _raw()
    raw["dimensions"][0]["source"] = "  "
    with pytest.raises(RubricError, match="empty source"):
        parse_rubric(raw)


def test_anchoring_outside_the_scale_does_not_load():
    raw = _raw()
    raw["dimensions"][0]["anchors"]["7"] = "beyond the scale"
    with pytest.raises(RubricError, match="outside the scale"):
        parse_rubric(raw)


def test_thresholds_must_sit_inside_the_scale_and_in_order():
    raw = _raw()
    raw["thresholds"] = {"revise_below": 2, "fail_below": 4}
    with pytest.raises(RubricError, match="thresholds must satisfy"):
        parse_rubric(raw)


def test_an_unknown_match_key_does_not_load():
    """Silently ignoring it would produce a rubric that claims to be
    specific and matches everyone."""
    raw = _raw()
    raw["match"] = {"min_hba1c": 9}
    with pytest.raises(RubricError, match="unknown match key"):
        parse_rubric(raw)


def test_duplicate_dimension_ids_do_not_load():
    raw = _raw()
    raw["dimensions"].append(copy.deepcopy(raw["dimensions"][0]))
    with pytest.raises(RubricError, match="duplicate dimension"):
        parse_rubric(raw)


def test_the_rubric_id_must_match_the_filename(tmp_path):
    raw = _raw()
    raw["rubric_id"] = "something-else"
    (tmp_path / "default.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RubricError, match="does not match the filename"):
        load_rubrics(tmp_path)


# ── archetype selection ──────────────────────────────────────────────────


def test_the_most_specific_matching_rubric_wins():
    assert select_rubric(ELDERLY).rubric_id == "multimorbid-elderly"


def test_a_patient_matching_nothing_specific_gets_the_fallback():
    assert select_rubric(_context(age=34)).rubric_id == "default"


def test_one_constraint_short_is_not_a_match():
    """78 years old and two problems, but only three medications — the
    archetype is "multimorbid AND polypharmacy", not "either"."""
    context = _context(
        age=78,
        problems=[("E11.9", "Type 2 diabetes mellitus", False), ("N18.4", "CKD", True)],
        medications=[("Metformin", "Biguanide"), ("Ramipril", "ACE"), ("Aspirin", "Antiplatelet")],
    )
    assert select_rubric(context).rubric_id == "default"


def test_selection_is_reproducible_when_specificity_ties():
    """A plan graded today and re-graded next month must be graded against
    the same rubric. "Whichever the filesystem listed first" is not that."""
    rubrics = load_rubrics()
    first = select_rubric(ELDERLY, rubrics).rubric_id
    assert select_rubric(ELDERLY, list(reversed(rubrics))).rubric_id == first


def test_selection_with_no_fallback_refuses_rather_than_guesses():
    only_specific = [r for r in load_rubrics() if r.match]
    with pytest.raises(RubricError, match="no rubric matches"):
        select_rubric(_context(age=30), only_specific)


# ── the facts, and the one claim they are careful not to make ────────────


def test_a_problem_absent_from_the_plan_is_reported_as_not_mentioned():
    evidence = _plan(concerns=[_cited(id=1, statement="Risk of hypoglycaemia from glipizide")])
    facts = compute_facts(evidence)
    assert facts.values["problems_not_mentioned"] == ["Type 2 diabetes mellitus", "Chronic kidney disease"]


def test_a_problem_the_plan_talks_about_is_not_reported():
    evidence = _plan(
        concerns=[_cited(id=1, statement="Type 2 diabetes mellitus is not controlled")],
        goals=[_cited(id=2, statement="Protect remaining kidney function", concern_id=1)],
    )
    assert compute_facts(evidence).values["problems_not_mentioned"] == []


def test_a_phrase_with_nothing_distinctive_is_never_called_missing():
    """Reporting "not mentioned" about something with no distinctive word
    to look for would be a finding manufactured by the check rather than
    found by it."""
    assert mentions("in the of a", "completely unrelated text") is True


def test_the_lexical_check_does_not_claim_to_judge_care():
    """It answers "does the plan talk about this at all". A plan that names
    the problem and does nothing useful about it still counts as
    mentioning it — deciding whether that is enough is the grader's job,
    and the fact is named so nobody mistakes one for the other."""
    evidence = _plan(concerns=[_cited(id=1, statement="Type 2 diabetes mellitus and chronic kidney disease")])
    assert compute_facts(evidence).values["problems_not_mentioned"] == []


def test_a_fact_the_chart_could_not_supply_is_not_reported_as_zero():
    """`vetoed` only exists at generation time. A plan read back months
    later has no record of it, and "not recorded" and "nothing was vetoed"
    are different statements."""
    facts = compute_facts(_plan())
    assert facts.values["vetoed"] is NOT_RECORDED
    assert render(facts.values["vetoed"]) == "not recorded"
    assert render([]) == "none"


def test_generation_time_reports_are_carried_through_when_present():
    report = ReconcileReport(vetoed=["Increase glipizide — flagged"], merged=[])
    facts = compute_facts(_plan(reconciliation=report))
    assert facts.values["vetoed"] == ["Increase glipizide — flagged"]
    assert facts.values["merged"] == []


def test_a_flag_the_plan_never_speaks_to_is_surfaced():
    flags = [
        RiskFlag("polypharmacy", "burden", "Polypharmacy worth reviewing", "5 drugs", "med_safety/other")
    ]
    evidence = _plan(concerns=[_cited(id=1, statement="Hypoglycaemia risk")], flags=flags)
    assert compute_facts(evidence).values["flags_not_mentioned"] == ["polypharmacy"]


def test_a_flag_the_plan_engages_with_by_citation_is_not_called_unmentioned():
    """The regression the first live plan produced.

    ``uncontrolled-chronic`` was reported as unmentioned for a plan that was
    entirely about the patient's uncontrolled diabetes — because the plan
    said "glycaemic" and "glucose-lowering" throughout and never once said
    "diabetes". A word comparison cannot see clinical synonymy; the citation
    graph does not have to, because it is exact.
    """
    flags = [
        RiskFlag(
            "uncontrolled-chronic",
            "disease_control",
            "Chronic condition recorded as not controlled",
            "Type 2 diabetes mellitus (E11.9)",
            "med_safety/glycaemic-targets-older-adults",
        )
    ]
    evidence = _plan(
        concerns=[
            Row(
                id=1,
                statement="Glycaemic therapy should be reviewed for deintensification",
                evidence_refs={"chunks": ["med_safety/glycaemic-targets-older-adults#0"]},
            )
        ],
        flags=flags,
    )
    assert compute_facts(evidence).values["flags_not_mentioned"] == []


def test_goals_and_burden_are_counted_from_the_written_rows():
    evidence = _plan(
        concerns=[_cited(id=1, statement="Hypoglycaemia risk")],
        goals=[
            _cited(
                id=10, statement="Avoid hypoglycaemia", concern_id=1, target_value="no episodes in 3 months"
            ),
            _cited(id=11, statement="Stay independent", concern_id=1),
        ],
        interventions=[_cited(id=20, statement="Review glipizide", goal_id=10)],
    )
    facts = compute_facts(evidence)
    assert facts.values["goal_count"] == 2
    assert facts.values["goals_with_target"] == 1
    assert facts.values["intervention_count"] == 1
    assert facts.values["burden_flagged"] is False
    assert facts.values["bare_goals"] == ["goal 11"]


def test_an_ai_element_with_no_citation_is_surfaced_and_a_human_one_is_not():
    """The structural check `validate()` already makes, restated as a fact
    so the grader is told rather than left to notice."""
    evidence = _plan(
        concerns=[Row(id=1, statement="AI concern", source="ai", evidence_refs={"chunks": []})],
        goals=[Row(id=2, statement="Clinician goal", source="clinician", concern_id=1)],
    )
    facts = compute_facts(evidence)
    assert facts.values["elements_without_evidence"] == ["concern 1"]


def test_an_element_pointing_outside_the_plan_is_surfaced():
    evidence = _plan(
        concerns=[_cited(id=1, statement="A concern")],
        goals=[_cited(id=2, statement="A goal", concern_id=999)],
    )
    assert compute_facts(evidence).values["orphan_elements"] == ["goal 2"]


def test_fact_lines_say_what_was_measured():
    """The grader reads these. A bare number invites it to interpret the
    name, and `problems_not_mentioned` means something narrower than it
    sounds."""
    lines = compute_facts(_plan()).as_lines(["problems_not_mentioned"])
    assert "lexical check only" in lines[0]


# ── scoring, and the two decisions that matter ───────────────────────────


DIMENSIONS = (
    "completeness",
    "traceability",
    "guideline_concordance",
    "safety",
    "goal_quality",
    "feasibility_burden",
)


def _evaluate(scores: dict):
    evidence = _plan(concerns=[_cited(id=1, statement="Type 2 diabetes mellitus is uncontrolled")])
    return evaluate(evidence, stub_grader(scores), situation="an older adult")


def test_a_plan_scoring_well_everywhere_passes():
    rubric, evaluation = _evaluate(dict.fromkeys(DIMENSIONS, 5))
    assert evaluation.verdict(rubric) == PASS
    assert evaluation.overall == 5.0


def test_the_worst_dimension_decides_the_verdict_not_the_mean():
    """The most important test in this file.

    Five 5s and a 1 average 4.33 — comfortably above the pass threshold on
    a 1-5 scale. But the 1 is on safety, and a plan that is excellent five
    ways and dangerous once is not a good plan with a blemish. Averaging is
    exactly the operation that hides that, so the mean is recorded for
    trend and the minimum governs.
    """
    scores = dict.fromkeys(DIMENSIONS, 5)
    scores["safety"] = 1
    rubric, evaluation = _evaluate(scores)
    assert evaluation.overall > rubric.revise_below
    assert evaluation.verdict(rubric) == FAIL


def test_a_middling_dimension_routes_to_revision():
    scores = dict.fromkeys(DIMENSIONS, 5)
    scores["goal_quality"] = 3
    rubric, evaluation = _evaluate(scores)
    assert evaluation.verdict(rubric) == REVISE
    assert evaluation.lowest.dimension_id == "goal_quality"


def test_a_dimension_that_could_not_be_graded_is_never_a_pass():
    """A pass nobody computed is not a pass."""
    scores = dict.fromkeys(DIMENSIONS, 5)
    del scores["safety"]
    rubric, evaluation = _evaluate(scores)
    assert [score.dimension_id for score in evaluation.ungraded] == ["safety"]
    assert evaluation.verdict(rubric) == REVISE


def test_an_out_of_scale_score_is_ungraded_rather_than_clamped():
    """Clamping a 9 to a 5 would invent agreement with the rubric that the
    grader never expressed."""
    scores = dict.fromkeys(DIMENSIONS, 5)
    scores["safety"] = 9
    rubric, evaluation = _evaluate(scores)
    safety = next(score for score in evaluation.scores if score.dimension_id == "safety")
    assert safety.score is None
    assert "outside the scale" in safety.ungraded_reason
    assert evaluation.verdict(rubric) == REVISE


def test_a_non_numeric_score_is_ungraded():
    scores = dict.fromkeys(DIMENSIONS, 5)
    scores["safety"] = {"score": "very good", "justification": "..."}
    _rubric, evaluation = _evaluate(scores)
    safety = next(score for score in evaluation.scores if score.dimension_id == "safety")
    assert safety.score is None
    assert "not a number" in safety.ungraded_reason


def test_one_dimension_raising_does_not_lose_the_others():
    def grader(task):
        if task.dimension.id == "safety":
            raise RuntimeError("upstream timeout")
        return {"score": 5, "justification": "fine"}

    evidence = _plan(concerns=[_cited(id=1, statement="A concern")])
    rubric, evaluation = evaluate(evidence, grader)
    assert len(evaluation.graded) == len(rubric.dimensions) - 1
    safety = next(score for score in evaluation.scores if score.dimension_id == "safety")
    assert "upstream timeout" in safety.ungraded_reason


def test_the_mean_is_taken_over_graded_dimensions_only():
    scores = dict.fromkeys(DIMENSIONS, 4)
    del scores["safety"]
    _rubric, evaluation = _evaluate(scores)
    assert evaluation.overall == 4.0


def test_nothing_graded_at_all_is_not_silently_a_pass():
    evaluation = Evaluation(rubric_id="default", rubric_version=1)
    rubric = next(r for r in load_rubrics() if r.rubric_id == "default")
    assert evaluation.verdict(rubric) != PASS


def test_the_grader_is_handed_the_facts_its_dimension_declared():
    """§9's whole instruction: deterministic checks are injected so the
    grader does not re-derive them."""
    seen = {}

    def grader(task):
        seen[task.dimension.id] = task
        return {"score": 4, "justification": "ok"}

    evidence = _plan(concerns=[_cited(id=1, statement="A concern")])
    rubric, _evaluation = evaluate(evidence, grader)
    safety_dimension = rubric.dimension("safety")
    task = seen["safety"]
    assert set(task.facts) == set(safety_dimension.facts)
    assert task.scale == (rubric.scale_min, rubric.scale_max)
    assert any("flags_fired" in line for line in task.fact_lines)


def test_the_grader_sees_the_plan_with_its_graph_intact():
    """Flat lists would make the grader infer which goal answers which
    concern — a structure the database knows exactly."""
    seen = []

    def grader(task):
        seen.append(task.plan_text)
        return {"score": 4, "justification": "ok"}

    evidence = _plan(
        concerns=[_cited(id=1, statement="Hypoglycaemia risk")],
        goals=[_cited(id=2, statement="Avoid hypoglycaemia", concern_id=1)],
        interventions=[_cited(id=3, statement="Review glipizide", goal_id=2, owner_role="prescriber")],
    )
    evaluate(evidence, grader)
    assert "CONCERN" in seen[0] and "  GOAL" in seen[0] and "    INTERVENTION" in seen[0]
    assert "[prescriber]" in seen[0]
