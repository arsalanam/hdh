"""Auto-evaluation: score a written plan against its rubric.

Design §9. The grading itself is one call per dimension, injected the same
way ``Selector`` is — so this whole module runs in CI with no API key, and
the milestone that builds the machinery is separable from the one that
builds the model call.

Three decisions are worth finding here rather than inferring.

**The minimum governs the verdict, not the mean.** A plan that scores well
on five dimensions and badly on safety is not a passing plan with a blemish;
it is an unsafe plan. Averaging is exactly the operation that hides that, so
``overall`` is recorded for reporting and trend, and the worst dimension
decides.

**A dimension that could not be graded never yields a pass.** A malformed or
out-of-scale response is not a zero and not an average — it is an unknown,
and a pass nobody computed is not a pass.

**Nothing here approves or rejects anything.** §9 says auto-evaluation
informs the human and never auto-approves; the mirror matters equally. A
``fail`` that quietly binned a plan would take away the decision the design
reserves for a person. The verdict is advisory in both directions, and the
plan's own status is untouched by grading.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hdh.modules.careplan.facts import PlanEvidence, PlanFacts, compute_facts
from hdh.modules.careplan.rubric import Dimension, Rubric, select_rubric

#: Verdicts, matching the ``plan_evaluations.verdict`` enum.
PASS, REVISE, FAIL = "pass", "revise", "fail"


@dataclass(frozen=True)
class GradingTask:
    """One dimension, everything needed to score it, and the answer shape."""

    dimension: Dimension
    situation: str
    plan_text: str
    facts: Mapping[str, object]
    fact_lines: tuple[str, ...]
    scale: tuple[int, int]
    schema: Mapping[str, object]


#: Given a task, return ``{"score": int, "justification": str}``. Injectable
#: for the same reason ``Selector`` is: the pipeline has to run in CI.
Grader = Callable[[GradingTask], dict]


@dataclass(frozen=True)
class DimensionScore:
    """One dimension's result, or the reason there isn't one."""

    dimension_id: str
    score: int | None
    justification: str = ""
    ungraded_reason: str = ""

    @property
    def graded(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "justification": self.justification,
            "ungraded_reason": self.ungraded_reason,
        }


@dataclass
class Evaluation:
    """What grading produced, and what it refused to conclude."""

    rubric_id: str
    rubric_version: int
    scores: list[DimensionScore] = field(default_factory=list)
    facts: PlanFacts | None = None

    @property
    def graded(self) -> list[DimensionScore]:
        return [score for score in self.scores if score.graded]

    @property
    def ungraded(self) -> list[DimensionScore]:
        return [score for score in self.scores if not score.graded]

    @property
    def overall(self) -> float | None:
        """The unweighted mean of the graded dimensions.

        Recorded for reporting and comparison over time. It does **not**
        decide the verdict — see :meth:`verdict`.
        """
        graded = self.graded
        if not graded:
            return None
        return round(sum(score.score for score in graded) / len(graded), 2)

    @property
    def lowest(self) -> DimensionScore | None:
        graded = self.graded
        return min(graded, key=lambda score: score.score) if graded else None

    def verdict(self, rubric: Rubric) -> str:
        """The governing dimension decides, and an unknown is never a pass."""
        if not self.graded:
            return FAIL if self.scores else REVISE
        worst = self.lowest
        if worst.score < rubric.fail_below:
            return FAIL
        if worst.score < rubric.revise_below or self.ungraded:
            return REVISE
        return PASS

    def narrative(self, rubric: Rubric) -> str:
        """A sentence a reviewer can read before opening anything else."""
        verdict = self.verdict(rubric)
        overall = "—" if self.overall is None else f"{self.overall}"
        worst = self.lowest
        parts = [f"{verdict} · mean {overall}/{rubric.scale_max} against {rubric.rubric_id}"]
        if worst is not None:
            parts.append(f"lowest: {worst.dimension_id} at {worst.score}")
        if self.ungraded:
            parts.append(f"{len(self.ungraded)} dimension(s) could not be graded")
        return " · ".join(parts)


def grade_schema(rubric: Rubric) -> dict:
    """The answer shape for one dimension, bounded by the rubric's scale."""
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": rubric.scale_min, "maximum": rubric.scale_max},
            "justification": {"type": "string"},
        },
        "required": ["score", "justification"],
        "additionalProperties": False,
    }


def render_plan(evidence: PlanEvidence) -> str:
    """The plan as the grader reads it, with its graph shape preserved.

    Flat lists would ask the grader to infer which goal answers which
    concern — a structure the database already knows exactly.
    """
    lines: list[str] = []
    for concern in evidence.concerns:
        lines.append(f"CONCERN [{concern.concern_type}] {concern.statement}")
        for goal in evidence.goals:
            if goal.concern_id != concern.id:
                continue
            target = f"  (target: {goal.target_value})" if goal.target_value else ""
            lines.append(f"  GOAL {goal.statement}{target}")
            for item in evidence.interventions:
                if item.goal_id != goal.id:
                    continue
                owner = f" [{item.owner_role}]" if item.owner_role else ""
                lines.append(f"    INTERVENTION ({item.intervention_type}) {item.statement}{owner}")
    return "\n".join(lines) or "(the plan has no elements)"


def _score_one(task: GradingTask, grader: Grader) -> DimensionScore:
    """One dimension, with every way the answer can be unusable handled.

    A grader that returns nonsense must not silently become a number. Each
    failure below produces an *ungraded* dimension, which the verdict then
    refuses to treat as a pass.
    """
    low, high = task.scale
    try:
        answer = grader(task)
    except Exception as err:  # noqa: BLE001 — one bad dimension must not lose the rest
        return DimensionScore(
            task.dimension.id, None, ungraded_reason=f"grader raised {type(err).__name__}: {err}"
        )
    if not isinstance(answer, Mapping) or "score" not in answer:
        return DimensionScore(task.dimension.id, None, ungraded_reason="grader returned no score")
    try:
        score = int(answer["score"])
    except (TypeError, ValueError):
        return DimensionScore(
            task.dimension.id, None, ungraded_reason=f"score {answer['score']!r} is not a number"
        )
    if not low <= score <= high:
        return DimensionScore(
            task.dimension.id, None, ungraded_reason=f"score {score} is outside the scale {low}-{high}"
        )
    return DimensionScore(task.dimension.id, score, str(answer.get("justification", "")).strip())


def evaluate(
    evidence: PlanEvidence,
    grader: Grader,
    *,
    rubric: Rubric | None = None,
    situation: str = "",
) -> tuple[Rubric, Evaluation]:
    """Score a plan against the rubric its archetype selects."""
    from hdh.modules.careplan.generate import situation as describe

    rubric = rubric or select_rubric(evidence.context)
    facts = compute_facts(evidence)
    plan_text = render_plan(evidence)
    described = situation or describe(evidence.context, evidence.flags)
    schema = grade_schema(rubric)

    evaluation = Evaluation(rubric_id=rubric.rubric_id, rubric_version=rubric.version, facts=facts)
    for dimension in rubric.dimensions:
        task = GradingTask(
            dimension=dimension,
            situation=described,
            plan_text=plan_text,
            facts=facts.subset(dimension.facts),
            fact_lines=tuple(facts.as_lines(dimension.facts)),
            scale=(rubric.scale_min, rubric.scale_max),
            schema=schema,
        )
        evaluation.scores.append(_score_one(task, grader))
    return rubric, evaluation


def stub_grader(answers: Mapping[str, object] | Sequence[object]) -> Grader:
    """A fixed set of answers — tests and offline demos, zero LLM.

    Accepts either a mapping keyed by dimension id or a positional
    sequence. A dimension with no answer comes back ungraded, which is the
    honest outcome and keeps the verdict logic exercised.
    """
    queue = list(answers) if not isinstance(answers, Mapping) else []

    def grade(task: GradingTask) -> dict:
        if isinstance(answers, Mapping):
            answer = answers.get(task.dimension.id)
        else:
            answer = queue.pop(0) if queue else None
        if answer is None:
            return {}
        if isinstance(answer, Mapping):
            return dict(answer)
        return {"score": answer, "justification": "stub"}

    return grade


def record_evaluation(session, plan_id: int, rubric: Rubric, evaluation: Evaluation) -> int:
    """Persist one evaluation; returns its id.

    The plan's own ``status`` is deliberately not touched. Grading informs
    the human who approves; it does not do the approving, and it does not
    do the rejecting either.
    """
    from sqlalchemy import insert

    from hdh.core.models import Base

    table = Base.metadata.tables["plan_evaluations"]
    scores = {score.dimension_id: score.as_dict() for score in evaluation.scores}
    for dimension_id, block in scores.items():
        dimension = rubric.dimension(dimension_id)
        block["facts"] = (
            {name: evaluation.facts.values.get(name) for name in dimension.facts}
            if dimension is not None and evaluation.facts is not None
            else {}
        )
    return int(
        session.execute(
            insert(table).returning(table.c.id),
            [
                {
                    "care_plan_id": plan_id,
                    "rubric_id": f"{rubric.rubric_id}@{rubric.version}",
                    "dimension_scores": scores,
                    "overall": evaluation.overall,
                    "verdict": evaluation.verdict(rubric),
                    "narrative": evaluation.narrative(rubric),
                    "evaluated_at": datetime.utcnow(),
                }
            ],
        ).scalar_one()
    )
