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

    @property
    def is_flag(self) -> bool:
        return self.priority == FLAG


def _flag_topics(flags: Sequence[RiskFlag]) -> list[Topic]:
    """Every fired flag becomes a topic.

    Flags are not capped. They are deterministic safety findings that the
    rules have already justified, they are bounded by the size of the rule
    set, and the rubric's safety dimension grades precisely whether the
    plan answered them. Deferring one would be deferring the part of the
    plan most likely to matter.
    """
    return [
        Topic(
            key=f"flag:{flag.rule_id}",
            label=flag.statement,
            query=f"{flag.statement}. {flag.basis}",
            priority=FLAG,
            basis=f"rule {flag.rule_id} fired: {flag.basis}",
        )
        for flag in flags
    ]


def _problem_topics(context: CarePlanContext, flags: Sequence[RiskFlag]) -> list[Topic]:
    """Chronic problems, uncontrolled first, minus those a flag already covers.

    The overlap is real rather than theoretical: the ``uncontrolled-chronic``
    rule's basis names the very problems that would otherwise become topics
    of their own, and two concerns about one condition is how a plan starts
    saying the same thing twice.

    **Matched on the ICD-10 code, never on the wording.** The first version
    used :func:`~hdh.modules.careplan.text.mentions`, which is an OR over
    significant words — and on the first real chart it dropped
    osteoarthritis and atrial fibrillation because both descriptions contain
    *"unspecified"*, which also appears in "Hyperlipidemia, unspecified",
    and dropped heart failure and chronic kidney disease on the word
    *"chronic"*, which appears in "Chronic condition recorded as not
    controlled". Four conditions silently removed from a plan by two of the
    least meaningful words in the sentence.

    A lexical OR is right for asking *"does the plan discuss this at all"*,
    where a false positive costs nothing. It is wrong for *"is this already
    covered"*, where a false positive deletes a topic. The code is exact,
    and ``uncontrolled-chronic`` already writes it into its basis.
    """
    covered = " ".join(flag.basis for flag in flags).lower()
    topics: list[Topic] = []
    for problem in context.problems:
        code = (problem.icd10 or "").lower()
        if code and code in covered:
            continue
        uncontrolled = problem.controlled is False
        topics.append(
            Topic(
                key=f"problem:{problem.icd10}",
                label=problem.description,
                query=problem.description,
                priority=UNCONTROLLED if uncontrolled else STABLE,
                basis=("recorded as not controlled" if uncontrolled else "chronic problem on the chart"),
            )
        )
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
    flag_topics = _flag_topics(flags)
    problem_topics = _problem_topics(context, flags)
    return flag_topics + problem_topics[:limit], problem_topics[limit:]


def deferral_lines(deferred: Sequence[Topic]) -> list[str]:
    """What a reader is told about the problems this plan did not take on."""
    return [f"{topic.label} — {topic.basis}, not addressed in this plan" for topic in deferred]
