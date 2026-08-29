"""S5b of `interactive-care-planning.md`: change the wording, see what moved.

One patient, two prompt sets, both plans generated and graded, laid side by
side. It is the fast loop — the one you run twenty times while working out
what a rubric dimension is actually rewarding.

**What this module is careful not to do.** It never says an edit was an
improvement.

A single case cannot clear the cohort's noise floor. The measured pooled
standard deviation is 0.207 on a 1–5 mean, and individual cases in the
baseline show per-case deviations of 0.19 to 0.26 — so two runs of the *same*
prompt on the *same* patient routinely differ by more than most edits will.
A tuning run that reported "+0.3, better" would be reporting noise with a
sign on it, and it would be believed, because the person reading it just
made the change and wants it to have worked.

So every summary this module produces ends by naming the floor and naming
the arbiter: the cohort, at three repeats, with a prompt-set version bump so
`compare` can tell the wording moved rather than the plans improving.

The loop this belongs to:

1. read a plan (`render.py`) and form a hypothesis
2. edit a prompt set, bump its version
3. **tune** — one patient, both sets, see what changed
4. `careplan eval run --repeat 3 --save` — the cohort decides
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: What a single case cannot do, stated wherever a number is shown.
CANNOT_DECIDE = (
    "A single case cannot decide this. Two runs of the same prompt on the "
    "same patient differ by about as much as this, so read the plans, not "
    "the delta."
)


@dataclass(frozen=True)
class Side:
    """One prompt set's attempt at the same patient."""

    prompt_set: str
    scores: Mapping[str, int] = field(default_factory=dict)
    verdict: str = ""
    concerns: int = 0
    goals: int = 0
    interventions: int = 0
    uncited: int = 0
    values: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mean(self) -> float | None:
        return round(sum(self.scores.values()) / len(self.scores), 3) if self.scores else None

    @property
    def governing(self) -> str:
        """The dimension that decided the verdict — the lowest, not the mean."""
        if not self.scores:
            return ""
        lowest = min(self.scores.values())
        return ", ".join(sorted(d for d, s in self.scores.items() if s == lowest))


@dataclass(frozen=True)
class TuneResult:
    """Both attempts, and the floor neither of them can clear alone."""

    mrn: str
    before: Side
    after: Side
    noise: float = 0.0

    def deltas(self) -> dict[str, int]:
        """Per-dimension movement, for dimensions both sides scored."""
        shared = set(self.before.scores) & set(self.after.scores)
        return {d: self.after.scores[d] - self.before.scores[d] for d in sorted(shared)}


def summarise(result: TuneResult) -> list[str]:
    """The comparison as lines, ending in what it does not establish.

    The refusal is the last thing rather than the first, deliberately: it has
    to be the sentence still on screen when someone decides what to do next.
    """
    before, after = result.before, result.after
    lines = [
        f"{result.mrn}: {before.prompt_set} -> {after.prompt_set}",
        "",
        f"  {'':<24}{before.prompt_set:>16}{after.prompt_set:>16}",
        f"  {'concerns':<24}{before.concerns:>16}{after.concerns:>16}",
        f"  {'goals':<24}{before.goals:>16}{after.goals:>16}",
        f"  {'interventions':<24}{before.interventions:>16}{after.interventions:>16}",
        f"  {'elements citing nothing':<24}{before.uncited:>16}{after.uncited:>16}",
    ]

    deltas = result.deltas()
    if deltas:
        lines.append("")
        lines.append(f"  {'dimension':<24}{'before':>16}{'after':>16}   change")
        for dimension, delta in deltas.items():
            arrow = f"{delta:+d}" if delta else "same"
            lines.append(
                f"  {dimension:<24}{before.scores[dimension]:>16}{after.scores[dimension]:>16}   {arrow}"
            )
        lines.append("")
        lines.append(f"  governing: {before.governing or '—'} -> {after.governing or '—'}")
        if before.mean is not None and after.mean is not None:
            lines.append(f"  mean {before.mean} -> {after.mean} ({after.mean - before.mean:+.3f})")

    lines.append("")
    lines.append(f"  {CANNOT_DECIDE}")
    if result.noise:
        lines.append(
            f"  The cohort's measured noise floor is {result.noise} pooled sd; this run measures none."
        )
    lines.append("  To decide: bump the prompt set version, then `hdh careplan eval run --repeat 3 --save`.")
    return lines


def _side(prompt_set_name: str, values: Mapping[str, Any], scores, verdict: str) -> Side:
    from hdh.modules.careplan.render import uncited, view_from_state

    view = view_from_state("—", values)
    return Side(
        prompt_set=prompt_set_name,
        scores=dict(scores or {}),
        verdict=verdict,
        concerns=len(values.get("concerns") or ()),
        goals=len(values.get("goals") or ()),
        interventions=len(values.get("interventions") or values.get("raw_interventions") or ()),
        uncited=len(uncited(view)),
        values=dict(values),
    )


def run_once(session, mrn: str, prompt_set_name: str, services=None, *, grade: bool = True) -> Side:
    """Generate one plan for one patient under one prompt set, and grade it.

    The prompt set is selected around the whole run rather than passed into
    each call, because the grading instruction is part of the set too: an
    edit that changed how plans are *graded* would otherwise be invisible.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from hdh.core.models import Patient
    from hdh.modules.careplan import prompts as prompt_module
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.graph import PlanServices, compile_pipeline, thread_config, to_draft
    from hdh.modules.careplan.rubric import select_rubric

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        raise ValueError(f"no patient {mrn}")

    with prompt_module.using(prompt_set_name) as active:
        context = build_context(session, patient)
        resolved = (services or PlanServices()).resolved(session)
        graph = compile_pipeline(checkpointer=InMemorySaver())
        config = thread_config(f"tune-{prompt_set_name}-{mrn}")
        values = graph.invoke({"context": context}, config, context=resolved)

        scores: dict[str, int] = {}
        verdict = ""
        if grade and resolved.grader is not None:
            from hdh.modules.careplan.evaluate import evaluate
            from hdh.modules.careplan.facts import evidence_from_draft

            evidence = evidence_from_draft(
                context,
                to_draft(values),
                flags=values.get("flags", ()),
                reconciliation=values.get("reconciliation"),
                deferred=values.get("deferred", ()),
            )
            rubric, evaluation = evaluate(evidence, resolved.grader, rubric=select_rubric(context))
            scores = {s.dimension_id: s.score for s in evaluation.scores if s.score is not None}
            verdict = evaluation.verdict(rubric)
        return _side(active.stamp, values, scores, verdict)


def tune(session, mrn: str, before: str, after: str, services=None, *, noise: float = 0.0):
    """Both sides of one wording change, on one patient."""
    return TuneResult(
        mrn=mrn,
        before=run_once(session, mrn, before, services),
        after=run_once(session, mrn, after, services),
        noise=noise,
    )


def cohort_noise(cohort: str = "default") -> float:
    """The floor a tuning run must not pretend to clear.

    Read from the saved baseline rather than written down here, so it stays
    the number actually measured rather than one that was true once.
    """
    from hdh.modules.careplan import evalset

    path = evalset.HERE / (f"baseline-{cohort}.json" if cohort != "default" else "baseline.json")
    try:
        return evalset.baseline_noise(evalset.load_baseline(path))
    except Exception:
        return 0.0


def written_pages(result: TuneResult, directory, sides: Sequence[Side] | None = None) -> list[str]:
    """Both plans as pages, so the change can be read rather than counted."""
    import pathlib

    from hdh.modules.careplan.render import Framing, view_from_state, write_plan_html

    out = pathlib.Path(directory)
    paths = []
    for side in sides or (result.before, result.after):
        safe = side.prompt_set.replace("@", "-v")
        view = view_from_state(
            result.mrn,
            side.values,
            Framing(scores=side.scores or None, verdict=side.verdict),
        )
        paths.append(
            write_plan_html(
                out / f"care-plan-{result.mrn.lower()}-{safe}.html",
                view,
                generated_note=f"Prompt set {side.prompt_set}. {CANNOT_DECIDE}",
            )
        )
    return paths
