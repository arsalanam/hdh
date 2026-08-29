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

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hdh.modules.careplan.facts import PlanEvidence, PlanFacts, compute_facts
from hdh.modules.careplan.prompts import prompt_set
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
    #: What kind of failure this was, stripped of anything per-call. The
    #: reason text carries a request id on API errors, which made six
    #: identical faults look like six different ones — see
    #: :attr:`Evaluation.common_failure`.
    ungraded_kind: str = ""

    @property
    def graded(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "justification": self.justification,
            "ungraded_reason": self.ungraded_reason,
            "ungraded_kind": self.ungraded_kind,
        }


@dataclass
class Evaluation:
    """What grading produced, and what it refused to conclude."""

    rubric_id: str
    rubric_version: int
    scores: list[DimensionScore] = field(default_factory=list)
    facts: PlanFacts | None = None

    def _scored(self) -> list[tuple[int, DimensionScore]]:
        """Graded dimensions paired with their score.

        The pairing is what carries the narrowing: ``score`` is optional on
        the dataclass, and a property that filters on it cannot tell a type
        checker that everything surviving the filter has a number. Reading
        the value out here says it once, in the one place that knows.
        """
        return [(score.score, score) for score in self.scores if score.score is not None]

    @property
    def graded(self) -> list[DimensionScore]:
        return [score for _value, score in self._scored()]

    @property
    def ungraded(self) -> list[DimensionScore]:
        return [score for score in self.scores if score.score is None]

    @property
    def overall(self) -> float | None:
        """The unweighted mean of the graded dimensions.

        Recorded for reporting and comparison over time. It does **not**
        decide the verdict — see :meth:`verdict`.
        """
        scored = self._scored()
        if not scored:
            return None
        return round(sum(value for value, _score in scored) / len(scored), 2)

    @property
    def lowest(self) -> DimensionScore | None:
        scored = self._scored()
        if not scored:
            return None
        return min(scored, key=lambda pair: pair[0])[1]

    @property
    def common_failure(self) -> str | None:
        """The one reason nothing could be graded, when there is only one.

        Six dimensions each reporting *"could not resolve authentication"*
        is not six judgements the grader was unable to reach — it is one
        environmental fault, laundered into a scorecard. A missing API key,
        an exhausted rate limit or a network outage hits every dimension
        identically, and saying so once is the difference between a
        diagnosis and a wall of noise.
        """
        if not self.scores or self.graded:
            return None
        # Grouped on `ungraded_kind`, never on the message. The first live
        # run failed all six dimensions on one malformed schema, and the
        # messages still differed — each carried its own API request id, so
        # comparing text found six distinct faults where there was one.
        if len({score.ungraded_kind for score in self.scores}) != 1:
            return None
        return self.scores[0].ungraded_reason

    def verdict(self, rubric: Rubric) -> str:
        """The governing dimension decides, and an unknown is never a pass."""
        scored = self._scored()
        if not scored:
            return FAIL if self.scores else REVISE
        worst = min(value for value, _score in scored)
        if worst < rubric.fail_below:
            return FAIL
        if worst < rubric.revise_below or self.ungraded:
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
    """The answer shape for one dimension, bounded by the rubric's scale.

    The bound is an ``enum`` of the levels, not ``minimum``/``maximum``.
    Partly because the API rejects the latter on integers — a live call
    returned *"For 'integer' type, properties maximum, minimum are not
    supported"* — and partly because an enum is the truer description: an
    anchored scale is a small set of named levels, each with a paragraph
    saying what it means, not a range with arbitrary points inside it.

    This is belt and braces with :func:`_score_one`, which rejects an
    out-of-scale score independently. Schema validation is the API's
    promise; the check is ours, and a promise nobody verifies is one
    nobody notices breaking.
    """
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "enum": list(range(rubric.scale_min, rubric.scale_max + 1))},
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
            task.dimension.id,
            None,
            ungraded_reason=f"grader raised {type(err).__name__}: {err}",
            ungraded_kind=type(err).__name__,
        )
    if not isinstance(answer, Mapping) or "score" not in answer:
        return DimensionScore(
            task.dimension.id, None, ungraded_reason="grader returned no score", ungraded_kind="no_score"
        )
    try:
        score = int(answer["score"])
    except (TypeError, ValueError):
        return DimensionScore(
            task.dimension.id,
            None,
            ungraded_reason=f"score {answer['score']!r} is not a number",
            ungraded_kind="not_a_number",
        )
    if not low <= score <= high:
        return DimensionScore(
            task.dimension.id,
            None,
            ungraded_reason=f"score {score} is outside the scale {low}-{high}",
            ungraded_kind="out_of_scale",
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


#: What the grader is told before it is shown anything.
#:
#: Three sentences here are load-bearing, and each stops a specific failure.
#:
#: The **facts are given** sentence is §9's whole instruction: the
#: deterministic checks exist so the grader does not re-derive them, and a
#: model asked to recount what it was already told will sometimes recount it
#: wrong.
#:
#: The **lexical** paragraph exists because the deterministic side made
#: exactly this mistake first. ``problems_not_mentioned`` reported eight
#: absent problems for a plan that discussed one of them throughout in
#: different words; a grader that reads that fact as a verdict rather than
#: as a word comparison will mark down a plan that handled the problem
#: perfectly well. The fact is named for what it measured — the prompt has
#: to make sure the reader honours the distinction.
#:
#: The **low levels are expected** sentence counteracts the pull toward the
#: middle of any scale. It is the same move the selector makes with
#: "returning fewer items, or none, is a valid and expected answer", and for
#: the same reason: without it the safe answer is always a 4.
#: The text itself now lives in `prompts/<set>.json` and is versioned with
#: the rest of the set. The commentary above stays here, next to the code
#: that depends on it — a JSON file is a poor place to explain why three
#: sentences are load-bearing.


def grading_prompt(task: GradingTask) -> str:
    """The prompt for one dimension. Separated so a test can read it."""
    return (
        prompt_set()
        .text("grading_instruction")
        .format(
            title=task.dimension.title,
            question=task.dimension.question,
            anchors="\n".join(task.dimension.anchor_lines()),
            situation=task.situation,
            plan=task.plan_text,
            facts="\n".join(task.fact_lines) or "(this dimension declares no facts)",
        )
    )


def llm_grader(model: str | None = None, client=None) -> Grader:
    """A Grader backed by Claude structured output, one call per dimension.

    Per dimension rather than per dimension group, which §9 allowed. Graded
    together, a strong showing on traceability bleeds into the safety score;
    graded alone, each dimension is answered on its own evidence. Six small
    calls also fail independently — :func:`_score_one` turns one bad
    response into one ungraded dimension instead of losing the evaluation.
    """
    import os

    from anthropic import Anthropic

    client = client or Anthropic()  # quality: allow(dependency-injection)
    resolved = model or os.environ.get("HDH_AGENT_MODEL", "claude-opus-5")

    def grade(task: GradingTask) -> dict:
        response = client.beta.messages.create(
            model=resolved,
            max_tokens=1000,
            messages=[{"role": "user", "content": grading_prompt(task)}],
            output_config={"format": {"type": "json_schema", "schema": dict(task.schema)}},
        )
        blocks = [block for block in response.content if block.type == "text"]
        return json.loads(blocks[0].text)

    return grade


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


class EvaluationError(RuntimeError):
    """An evaluation that must not be written as one."""


def record_evaluation(session, plan_id: int, rubric: Rubric, evaluation: Evaluation) -> int:
    """Persist one evaluation; returns its id.

    The plan's own ``status`` is deliberately not touched. Grading informs
    the human who approves; it does not do the approving, and it does not
    do the rejecting either.

    Raises:
        EvaluationError: nothing was graded. The verdict enum has no value
            for *"not evaluated"*, so an evaluation where every dimension
            failed would persist as ``fail`` — a row asserting that a plan
            was judged and found wanting, when what happened is that the
            API key was missing. That is the confident guess dressed as a
            measurement this module refuses everywhere else, and it would
            be attached to a patient's care plan. The guard lives here
            rather than in the callers so none of them can skip it.
    """
    from sqlalchemy import insert

    if not evaluation.graded:
        reason = evaluation.common_failure or "no dimension could be graded"
        raise EvaluationError(f"nothing was graded, so there is no evaluation to record — {reason}")

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
