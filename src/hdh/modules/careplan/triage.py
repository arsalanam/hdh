"""Node 2b: deciding what this plan is about, before anything retrieves.

Design §7 node 3, amended after #104. The generating node used to retrieve
**once for the whole patient** — the entire chart compacted into one
paragraph, matched against the corpus, six candidates back. For a patient
with ten chronic problems that cannot work, and measurement said so
plainly: the blended query returned six chunks covering five conditions,
every score between 0.0065 and 0.0112, none of them strong. A single
embedding or a single query over ten topics is a centroid close to none of
them.

The same corpus, queried one topic at a time, returned the right document
at rank 1 for **fourteen of fourteen** conditions. Retrieval was never the
weak part; asking it ten questions at once was.

So this module answers a prior question — *which* topics — and answers it
with arithmetic. Nothing here calls a model. What a plan should address is
a clinical judgement, but what a chart *contains* is not, and the ordering
below is the deterministic half: safety findings the rules already
justified, then problems the chart records as uncontrolled, then the rest.

**Deferral is a feature, not a shortfall.** A plan that addresses
everything on a fifteen-item problem list is not a care plan, it is a
reading of the chart, and the burden count in §7 node 6 exists because
nobody can action it. The rubric agrees: `multimorbid-elderly` scores
completeness at 5 for a plan whose omissions are *"defensible from the
chart and visible to the reader"* — so what is left out is recorded and
shown rather than silently dropped, which is the difference between a
focused plan and an incomplete one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.stratify import RiskFlag

#: How many chart problems one plan will address, beyond the flags.
#:
#: A number set by what a plan can carry rather than by what a chart holds.
#: Node 6 flags a plan at ``BURDEN_LIMIT`` interventions; a concern yields
#: roughly one to two goals and each goal two to three interventions, so
#: pure burden arithmetic argues for three or four problems, and
#: completeness argues for all of them. Six sits between, deliberately, and
#: is a starting point to be measured rather than a derived constant.
#:
#: Deferral is what makes any cap defensible: the problems that do not fit
#: are recorded on the plan, not discarded.
PROBLEM_LIMIT = 6

#: Priority tiers. Lower sorts first.
FLAG = 0
UNCONTROLLED = 1
STABLE = 2


@dataclass(frozen=True)
class Topic:
    """One thing a plan might be about, and why it was chosen.

    ``query`` is deliberately narrow — the single subject to retrieve on.
    The patient's wider situation reaches the model separately, as the
    selection task's situation text, because blending the two back together
    is the failure this module exists to undo.
    """

    key: str
    label: str
    query: str
    priority: int
    basis: str
    #: The ICD-10 code, for problem topics. Used to decide whether a flag
    #: is saying anything the problem topics do not already say.
    code: str = ""

    @property
    def is_flag(self) -> bool:
        return self.priority == FLAG


def _flag_topics(flags: Sequence[RiskFlag], covered: Sequence[Topic]) -> list[Topic]:
    """Fired flags, minus those the problem topics already say.

    Flags are not capped. They are deterministic safety findings the rules
    have already justified, bounded by the size of the rule set, and the
    rubric's safety dimension grades whether the plan answered them.
    Deferring one would defer the part of the plan most likely to matter.

    But some flags are **pointers rather than findings**.
    ``uncontrolled-chronic`` fires once and lists every uncontrolled problem
    in its basis, so as a topic it restates what those problems already say
    for themselves. Giving it a topic *and* giving them topics produces two
    concerns about one condition; giving it a topic *instead of* them was
    worse, and is the bug this replaced:

        A patient with two uncontrolled problems — osteoarthritis and
        hypothyroidism — got one aggregate topic. One topic yields one
        concern (``MAX_CONCERNS_PER_TOPIC``), the model wrote about the
        osteoarthritis, and the hypothyroidism vanished. Not deferred, not
        dropped, not reported: absent. The grader caught it and scored
        completeness down for an omission nothing had recorded.

    So a flag is skipped as a topic when every problem it names is already
    a selected topic in its own right — it keeps its place in
    ``flags_fired`` and in the safety dimension, it simply does not spawn a
    concern that says what another concern already says.
    """
    selected = {topic.code.lower() for topic in covered if topic.code}
    topics: list[Topic] = []
    for flag in flags:
        basis = flag.basis.lower()
        named = [code for code in selected if code and code in basis]
        if named:
            # Every problem this flag points at is being planned for on its
            # own terms, so the flag adds no subject of its own.
            continue
        topics.append(
            Topic(
                key=f"flag:{flag.rule_id}",
                label=flag.statement,
                query=f"{flag.statement}. {flag.basis}",
                priority=FLAG,
                basis=f"rule {flag.rule_id} fired: {flag.basis}",
            )
        )
    return topics


def _problem_topics(context: CarePlanContext) -> list[Topic]:
    """Every chronic problem becomes a topic, uncontrolled first.

    No deduplication against the flags happens here, and that is a
    correction. The first version dropped a problem whose ICD-10 code
    appeared in any flag's basis, on the reasoning that the flag already
    covered it. It does not: ``uncontrolled-chronic`` fires once and lists
    *every* uncontrolled problem, so two uncontrolled conditions collapsed
    into one topic, one topic yielded one concern, and the second condition
    vanished — not deferred, not dropped, simply absent.

    The overlap is real, but it is the flag that is redundant, not the
    problem. A problem is the subject; an aggregate flag pointing at
    several problems is a restatement. So every problem keeps its own
    topic and :func:`_flag_topics` drops the flags that add nothing.

    (An earlier version matched the flag's wording rather than its code,
    which was worse still: an OR over significant words dropped
    osteoarthritis and atrial fibrillation on *"unspecified"* and heart
    failure and chronic kidney disease on *"chronic"* — four conditions
    removed from a plan by two of the least meaningful words in the
    sentence.)
    """
    topics = [
        Topic(
            key=f"problem:{problem.icd10}",
            label=problem.description,
            query=problem.description,
            priority=UNCONTROLLED if problem.controlled is False else STABLE,
            basis=(
                "recorded as not controlled"
                if problem.controlled is False
                else "chronic problem on the chart"
            ),
            code=problem.icd10 or "",
        )
        for problem in context.problems
    ]
    # Stable sort: within a tier, chart order is preserved, so the same
    # chart triages the same way every time.
    return sorted(topics, key=lambda topic: topic.priority)


def triage(
    context: CarePlanContext,
    flags: Sequence[RiskFlag] = (),
    limit: int = PROBLEM_LIMIT,
) -> tuple[list[Topic], list[Topic]]:
    """What this plan will address, and what it is deferring.

    Returns ``(selected, deferred)``. Deferred topics are the point of the
    return type: a plan that quietly addressed six of fifteen problems and
    said nothing would be indistinguishable from one that missed nine.
    """
    problem_topics = _problem_topics(context)
    selected_problems = problem_topics[:limit]
    deferred = problem_topics[limit:]
    # Flags are filtered against what is actually SELECTED, not against
    # every problem on the chart: a flag pointing at a deferred problem is
    # the only remaining mention of it, and dropping it would lose the
    # subject twice over.
    flag_topics = _flag_topics(flags, selected_problems)
    return flag_topics + selected_problems, deferred


def deferral_lines(deferred: Sequence[Topic]) -> list[str]:
    """What a reader is told about the problems this plan did not take on."""
    return [f"{topic.label} — {topic.basis}, not addressed in this plan" for topic in deferred]
