"""Care plan, milestone 3b: the grader, and what it is told.

M3a built everything around the hole this fills, so the surface here is
small: a prompt, one call per dimension, and the parsing. What is worth
testing is not the call — it is **what reaches the model**, because every
way this can go quietly wrong is a way the prompt can be incomplete.

The live test is marked `llm` and never runs in `just qa` or CI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from hdh.modules.careplan.context import CarePlanContext, MedicationView, ProblemView
from hdh.modules.careplan.evaluate import (
    GradingTask,
    evaluate,
    grade_schema,
    grading_prompt,
    llm_grader,
)
from hdh.modules.careplan.facts import PlanEvidence, compute_facts
from hdh.modules.careplan.rubric import load_rubrics, select_rubric
from hdh.modules.careplan.stratify import RiskFlag

ELDERLY = CarePlanContext(
    mrn="TEST01",
    age=84,
    sex="MALE",
    problems=(
        ProblemView("E11.9", "Type 2 diabetes mellitus", False, None),
        ProblemView("N18.4", "Chronic kidney disease stage 3b", True, None),
    ),
    medications=(
        MedicationView("Glipizide", "Sulfonylurea", "5 mg", None),
        MedicationView("Metformin", "Biguanide", "1 g", None),
        MedicationView("Ramipril", "ACE inhibitor", "5 mg", None),
        MedicationView("Atorvastatin", "Statin", "40 mg", None),
        MedicationView("Aspirin", "Antiplatelet", "75 mg", None),
    ),
)

FLAGS = (
    RiskFlag(
        "sulfonylurea-in-older-adult",
        "medication_safety",
        "Risk of hypoglycaemia from a sulfonylurea in an older adult",
        "Glipizide at age 84",
        "med_safety/sulfonylurea-older-adults",
    ),
)


@dataclass
class Row:
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


def _evidence() -> PlanEvidence:
    refs = {"chunks": ["med_safety/sulfonylurea-older-adults#0"]}
    return PlanEvidence(
        context=ELDERLY,
        flags=FLAGS,
        concerns=(Row(id=1, statement="Hypoglycaemia risk from glipizide", evidence_refs=refs),),
        goals=(
            Row(
                id=2,
                statement="Patient has no unwitnessed hypoglycaemic episodes",
                concern_id=1,
                target_value="none in 3 months",
                evidence_refs=refs,
            ),
        ),
        interventions=(
            Row(
                id=3,
                statement="Reduce or discontinue glipizide",
                goal_id=2,
                intervention_type="medication",
                owner_role="prescriber",
                evidence_refs=refs,
            ),
        ),
    )


def _task(dimension_id: str = "safety") -> GradingTask:
    evidence = _evidence()
    rubric = select_rubric(ELDERLY)
    dimension = rubric.dimension(dimension_id)
    assert dimension is not None
    facts = compute_facts(evidence)
    from hdh.modules.careplan.evaluate import render_plan

    return GradingTask(
        dimension=dimension,
        situation="an 84-year-old on a sulfonylurea",
        plan_text=render_plan(evidence),
        facts=facts.subset(dimension.facts),
        fact_lines=tuple(facts.as_lines(dimension.facts)),
        scale=(rubric.scale_min, rubric.scale_max),
        schema=grade_schema(rubric),
    )


class _FakeClient:
    """Captures the request instead of making it."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        outer = self

        class _Block:
            type = "text"

            def __init__(self, text: str) -> None:
                self.text = text

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = [_Block(text)]

        class _Messages:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                return _Response(json.dumps(outer.payload))

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


# ── what reaches the model ───────────────────────────────────────────────


def test_the_prompt_carries_every_anchor_on_the_scale():
    """A level the grader was never shown cannot be chosen, and the point
    of an anchored scale is that each level has a description."""
    task = _task()
    prompt = grading_prompt(task)
    for level, text in task.dimension.anchors.items():
        assert text in prompt, f"level {level} missing from the prompt"


def test_the_prompt_carries_the_precomputed_facts():
    """§9's instruction: deterministic checks are injected so the grader
    does not re-derive them. A fact computed and not sent is a fact the
    model works out for itself, wrongly and at cost."""
    task = _task()
    prompt = grading_prompt(task)
    assert task.fact_lines, "the safety dimension declares facts"
    for line in task.fact_lines:
        assert line in prompt


def test_the_prompt_warns_that_lexical_facts_are_lexical():
    """The regression the deterministic side hit first.

    `problems_not_mentioned` reported eight absent problems for a plan that
    discussed one of them throughout in different words. A grader reading
    that as a verdict rather than a word comparison would mark down a plan
    that handled the problem perfectly well, so the caveat travels with the
    facts rather than living only in the fact's name.
    """
    prompt = grading_prompt(_task("completeness"))
    assert "lexical" in prompt.lower()
    assert "does not SAY" in prompt and "does not HANDLE" in prompt


def test_the_prompt_says_a_low_score_is_expected():
    """Without this the safe answer on any 1-5 scale is a 4. Same move the
    selector makes with "returning none is a valid answer"."""
    assert "expected answer" in grading_prompt(_task())


def test_the_prompt_carries_the_plan_and_the_situation():
    task = _task()
    prompt = grading_prompt(task)
    assert "Reduce or discontinue glipizide" in prompt
    assert task.situation in prompt


def test_a_dimension_with_no_facts_says_so_rather_than_sending_a_blank():
    task = _task()
    bare = GradingTask(
        dimension=task.dimension,
        situation=task.situation,
        plan_text=task.plan_text,
        facts={},
        fact_lines=(),
        scale=task.scale,
        schema=task.schema,
    )
    assert "declares no facts" in grading_prompt(bare)


# ── the call ─────────────────────────────────────────────────────────────


def test_the_grader_bounds_the_score_with_an_enum_of_the_levels():
    """Not `minimum`/`maximum` — a live call rejected those outright:
    "For 'integer' type, properties maximum, minimum are not supported".
    An enum is also the truer description of an anchored scale, which is a
    small set of named levels rather than a range with points inside it.

    Structured output is the first line of defence against a score of 9;
    `_score_one` is the second. Both exist because either alone can fail —
    and the first one did, on the first live run.
    """
    client = _FakeClient({"score": 4, "justification": "ok"})
    task = _task()
    llm_grader(model="test-model", client=client)(task)
    schema = client.calls[0]["output_config"]["format"]["schema"]
    low, high = task.scale
    assert schema["properties"]["score"]["enum"] == list(range(low, high + 1))
    assert "minimum" not in schema["properties"]["score"]
    assert "maximum" not in schema["properties"]["score"]
    assert schema["required"] == ["score", "justification"]


def test_the_grader_parses_the_answer():
    client = _FakeClient({"score": 2, "justification": "the sulfonylurea is untouched"})
    answer = llm_grader(model="test-model", client=client)(_task())
    assert answer == {"score": 2, "justification": "the sulfonylurea is untouched"}


def test_the_model_comes_from_the_environment_when_not_given(monkeypatch):
    monkeypatch.setenv("HDH_AGENT_MODEL", "claude-from-env")
    client = _FakeClient({"score": 3, "justification": "ok"})
    llm_grader(client=client)(_task())
    assert client.calls[0]["model"] == "claude-from-env"


def test_one_call_is_made_per_dimension_not_one_for_the_lot():
    """Graded together, a strong showing on traceability bleeds into the
    safety score. Graded alone, each dimension is answered on its own
    evidence — and one bad response costs one dimension, not the run."""
    client = _FakeClient({"score": 4, "justification": "ok"})
    rubric, evaluation = evaluate(_evidence(), llm_grader(model="test-model", client=client))
    assert len(client.calls) == len(rubric.dimensions)
    assert len(evaluation.graded) == len(rubric.dimensions)


def test_constructing_a_real_grader_client_is_blocked_in_tests():
    """The factory builds its own client, so the billing guard covers it —
    this is the call that would otherwise start charging on every qa run."""
    pytest.importorskip("anthropic")
    with pytest.raises(AssertionError, match="billable API call"):
        llm_grader()


# ── live ─────────────────────────────────────────────────────────────────


@pytest.mark.llm
def test_a_live_grader_scores_within_the_scale_and_explains_itself():
    """On demand only. Asserts the contract, not a particular score — the
    scores themselves are a judgement, and pinning one would make this test
    a record of what the model said once rather than a check of anything.
    """
    rubric = select_rubric(ELDERLY)
    _rubric, evaluation = evaluate(_evidence(), llm_grader())
    assert evaluation.graded, "nothing was graded"
    for score in evaluation.graded:
        assert rubric.scale_min <= score.score <= rubric.scale_max
        assert score.justification.strip(), f"{score.dimension_id} scored without explaining itself"
    assert evaluation.verdict(rubric) in {"pass", "revise", "fail"}


def test_the_bundled_rubrics_all_produce_usable_prompts():
    """Every dimension of every shipped rubric must render — a rubric that
    loads but cannot be turned into a prompt fails at the worst moment."""
    evidence = _evidence()
    for rubric in load_rubrics():
        _r, evaluation = evaluate(
            evidence,
            lambda task: {"score": task.scale[0], "justification": grading_prompt(task)[:20]},
            rubric=rubric,
        )
        assert len(evaluation.graded) == len(rubric.dimensions)
