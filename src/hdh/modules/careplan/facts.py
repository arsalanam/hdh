"""What the grader is told, rather than asked.

Design §9: *"Deterministic checks are injected as pre-computed facts so the
grader doesn't re-derive them."* Everything in this module is arithmetic or
a lexical check over rows that already exist — no model, no judgement.

The naming rule here matters more than it looks. A fact is named for **what
was measured**, never for what it implies. The check that compares a
problem's wording against the plan's text is called
``problems_not_mentioned``, not ``problems_unaddressed`` — because "not
mentioned" is exactly what a word comparison establishes, and whether an
unmentioned problem is genuinely unaddressed is a clinical judgement. That
judgement is the grader's job. Handing it a fact that has already made the
leap would be feeding it a confident guess dressed as a measurement, which
is the failure this module spends most of its code avoiding.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.reconcile import BURDEN_LIMIT, STOPWORDS, ReconcileReport
from hdh.modules.careplan.stratify import RiskFlag

#: Shorter than this and a word is too common to indicate that a plan is
#: talking about the same thing. Four is the shortest clinically
#: distinctive length in practice — "COPD", "gout", "type" all qualify.
_SIGNIFICANT = 4

#: A value the chart could not supply. Distinct from zero, and rendered as
#: "not recorded" so the grader is never told that nothing happened when
#: what actually happened is that nobody wrote it down.
NOT_RECORDED = None


@dataclass(frozen=True)
class PlanEvidence:
    """Everything the deterministic facts are computed from.

    ``reconciliation`` is optional because it only exists at generation
    time: a plan read back from the database months later has rows but no
    record of what node 6 removed. Facts that depend on it report
    ``NOT_RECORDED`` rather than zero.
    """

    context: CarePlanContext
    flags: tuple[RiskFlag, ...] = ()
    concerns: tuple = ()
    goals: tuple = ()
    interventions: tuple = ()
    reconciliation: ReconcileReport | None = None

    @property
    def plan_text(self) -> str:
        """Every statement in the plan, lowercased, for lexical checks."""
        rows = (*self.concerns, *self.goals, *self.interventions)
        return " ".join(str(getattr(row, "statement", "") or "") for row in rows).lower()


@dataclass(frozen=True)
class Fact:
    """One computed value and the sentence that explains what it measured."""

    name: str
    describe: str
    compute: Callable[[PlanEvidence], object]


@dataclass
class PlanFacts:
    """Computed facts, keyed by name."""

    values: dict[str, object] = field(default_factory=dict)

    def subset(self, names: Sequence[str]) -> dict[str, object]:
        return {name: self.values[name] for name in names if name in self.values}

    def as_lines(self, names: Sequence[str] | None = None) -> list[str]:
        """The facts as the grader sees them — value and what it measured."""
        chosen = names if names is not None else list(self.values)
        lines = []
        for name in chosen:
            if name not in self.values:
                continue
            lines.append(f"{name} = {render(self.values[name])}  ({FACTS[name].describe})")
        return lines


def render(value: object) -> str:
    """One fact value as text, with ``None`` never reading as zero."""
    if value is NOT_RECORDED:
        return "not recorded"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "none" if not value else "; ".join(str(item) for item in value)
    return str(value)


# ── the lexical check, and its one honest claim ──────────────────────────


def _significant_words(phrase: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z]+", phrase.lower())
        if word not in STOPWORDS and len(word) >= _SIGNIFICANT
    }


def mentions(phrase: str, haystack: str) -> bool:
    """Does ``haystack`` use any distinctive word from ``phrase``?

    Lexical only, and deliberately shallow. It answers *"does the plan talk
    about this at all"* — not *"does the plan handle this correctly"*,
    which no word comparison can answer.

    A phrase with no distinctive words returns True. Reporting *"not
    mentioned"* about something with nothing to look for would be a finding
    manufactured by the check rather than found by it.
    """
    words = _significant_words(phrase)
    if not words:
        return True
    return any(word in haystack for word in words)


def _not_mentioned(phrases: Sequence[str], haystack: str) -> list[str]:
    return [phrase for phrase in phrases if not mentions(phrase, haystack)]


# ── the registry ─────────────────────────────────────────────────────────


def _rec(evidence: PlanEvidence, attribute: str, default=NOT_RECORDED):
    report = evidence.reconciliation
    return default if report is None else getattr(report, attribute)


def _evidence_refs(row) -> list[str]:
    return list((getattr(row, "evidence_refs", None) or {}).get("chunks") or [])


def _without_evidence(evidence: PlanEvidence) -> list[str]:
    missing = []
    for label, rows in (
        ("concern", evidence.concerns),
        ("goal", evidence.goals),
        ("intervention", evidence.interventions),
    ):
        for row in rows:
            if getattr(row, "source", None) == "ai" and not _evidence_refs(row):
                missing.append(f"{label} {getattr(row, 'id', '?')}")
    return missing


def _orphans(evidence: PlanEvidence) -> list[str]:
    concern_ids = {getattr(row, "id", None) for row in evidence.concerns}
    goal_ids = {getattr(row, "id", None) for row in evidence.goals}
    orphans = [
        f"goal {goal.id}" for goal in evidence.goals if getattr(goal, "concern_id", None) not in concern_ids
    ]
    orphans += [
        f"intervention {item.id}"
        for item in evidence.interventions
        if getattr(item, "goal_id", None) not in goal_ids
    ]
    return orphans


def _bare_goals(evidence: PlanEvidence) -> list[str]:
    served = {getattr(item, "goal_id", None) for item in evidence.interventions}
    return [
        f"goal {getattr(goal, 'id', '?')}"
        for goal in evidence.goals
        if getattr(goal, "id", None) not in served
    ]


def _flags_not_engaged(evidence: PlanEvidence) -> list[str]:
    """Flags the plan neither talks about nor cites the guidance for.

    The lexical half of this alone was measurably wrong. On the first live
    plan it reported ``uncontrolled-chronic`` as unmentioned — for a plan
    that was *entirely* about the patient's uncontrolled diabetes, and said
    so throughout in the words "glycaemic" and "glucose-lowering" without
    ever using the word "diabetes". Clinical synonymy is exactly what a
    word comparison cannot see.

    The citation graph can. A flag names the document that explains why it
    matters; a plan that cites that same document is demonstrably engaging
    with it, and that is an exact fact rather than a lexical guess. So the
    check asks both questions and only reports a flag that fails both.
    """
    cited = {ref.split("#")[0] for ref in _citations(evidence)}
    return [
        flag.rule_id
        for flag in evidence.flags
        if flag.cites.split("#")[0] not in cited
        and not mentions(flag.statement, evidence.plan_text)
        and not mentions(flag.basis, evidence.plan_text)
    ]


def _citations(evidence: PlanEvidence) -> list[str]:
    refs: list[str] = []
    for row in (*evidence.concerns, *evidence.goals, *evidence.interventions):
        refs.extend(ref for ref in _evidence_refs(row) if ref not in refs)
    return refs


_REGISTRY: tuple[Fact, ...] = (
    Fact(
        "problem_count",
        "chronic problems recorded in the chart",
        lambda e: len(e.context.problems),
    ),
    Fact(
        "uncontrolled_count",
        "chronic problems the chart records as not controlled",
        lambda e: len(e.context.uncontrolled),
    ),
    Fact(
        "problems_not_mentioned",
        "chronic problems whose wording appears nowhere in the plan (lexical check only — "
        "it shows what the plan does not talk about, not what it fails to handle)",
        lambda e: _not_mentioned([p.description for p in e.context.problems], e.plan_text),
    ),
    Fact(
        "flags_fired",
        "deterministic risk flags raised by node 2 for this patient",
        lambda e: [f"{flag.rule_id}: {flag.statement}" for flag in e.flags],
    ),
    Fact(
        "flags_not_mentioned",
        "risk flags the plan neither discusses in words nor engages with by citing the same "
        "guidance the flag cites",
        _flags_not_engaged,
    ),
    Fact("concern_count", "health concerns in the plan", lambda e: len(e.concerns)),
    Fact("goal_count", "goals in the plan", lambda e: len(e.goals)),
    Fact(
        "goals_with_target",
        "goals carrying a measurable target value",
        lambda e: sum(1 for goal in e.goals if (getattr(goal, "target_value", None) or "").strip()),
    ),
    Fact("intervention_count", "interventions the plan asks of this patient", lambda e: len(e.interventions)),
    Fact(
        "burden_limit", "the count at or above which node 6 flags a plan as too heavy", lambda e: BURDEN_LIMIT
    ),
    Fact(
        "burden_flagged",
        "whether the intervention count reached the burden limit",
        lambda e: len(e.interventions) >= BURDEN_LIMIT,
    ),
    Fact("bare_goals", "goals with no intervention of their own", _bare_goals),
    Fact("citations", "distinct knowledge chunks the plan cites", _citations),
    Fact("elements_without_evidence", "AI-authored elements carrying no citation", _without_evidence),
    Fact("orphan_elements", "elements pointing outside this plan's graph", _orphans),
    Fact(
        "vetoed",
        "interventions node 6 removed for contradicting a flag (generation-time only)",
        lambda e: _rec(e, "vetoed"),
    ),
    Fact(
        "merged",
        "interventions node 6 removed as duplicates (generation-time only)",
        lambda e: _rec(e, "merged"),
    ),
)

#: Every fact a rubric may ask for, by name. A rubric naming anything not
#: in here fails to load — a typo that silently produced an empty fact
#: block would leave the grader guessing exactly what §9 says to tell it.
FACTS: Mapping[str, Fact] = {fact.name: fact for fact in _REGISTRY}


def compute_facts(evidence: PlanEvidence, names: Sequence[str] | None = None) -> PlanFacts:
    """Every requested fact (all of them by default)."""
    wanted = list(FACTS) if names is None else [name for name in names if name in FACTS]
    return PlanFacts({name: FACTS[name].compute(evidence) for name in wanted})


def gather(
    session,
    plan_id: int,
    context: CarePlanContext,
    flags: Sequence[RiskFlag] = (),
    *,
    reconciliation: ReconcileReport | None = None,
) -> PlanEvidence:
    """Read a written plan back out as evidence for grading."""
    from sqlalchemy import select

    from hdh.core.models import Base

    tables = Base.metadata.tables
    concerns = tables["health_concerns"]
    goals = tables["plan_goals"]
    interventions = tables["plan_interventions"]

    concern_rows = tuple(session.execute(select(concerns).where(concerns.c.care_plan_id == plan_id)).all())
    goal_rows = tuple(session.execute(select(goals).where(goals.c.care_plan_id == plan_id)).all())
    intervention_rows = tuple(
        session.execute(select(interventions).where(interventions.c.care_plan_id == plan_id)).all()
    )
    return PlanEvidence(
        context=context,
        flags=tuple(flags),
        concerns=concern_rows,
        goals=goal_rows,
        interventions=intervention_rows,
        reconciliation=reconciliation,
    )
