"""Node 6: the step where naive systems fail.

Design §7 calls it that, and the first live run proved it. Generating
concerns, goals and interventions independently produced **25
interventions** for one patient — five of them variations on *"establish a
monitoring arrangement"* and four on *"review whether glipizide remains
appropriate"*. Every one was individually reasonable, correctly grounded,
and cited real evidence. The plan was still unusable, because nobody can
action 25 items and most of them were the same three items said five ways.

Three passes, all deterministic:

1. **de-duplicate** across goals — the same action proposed for two goals
   is one action
2. **veto** — a plan that flags a drug as dangerous may not then propose
   more of it
3. **burden** — count what is being asked of the patient, and say so

Nothing here asks a model. §7 suggests an LLM pass to flag excessive
burden; a count answers that question exactly, and asking a model *"is 25
too many?"* would add cost and non-determinism to arithmetic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from hdh.modules.careplan.generate import InterventionDraft
from hdh.modules.careplan.stratify import RiskFlag

#: Words carrying no distinguishing information in a clinical instruction.
_STOPWORDS = frozenset(
    "the a an and or of to for in on with that this is are be as by given if it its "
    "at any so whether still been was were has have had not no can could should".split()
)

#: How many leading content words identify the ACTION.
#:
#: Measured on the 25-intervention live plan. Whole-statement overlap does
#: not separate: real duplicates peaked at Jaccard 0.474, because the model
#: writes a short action followed by a long, differently-worded rationale
#: — so the rationale dilutes exactly the signal being looked for. Compared
#: on the first five content words instead, exact-action repeats score 1.00
#: and paraphrases ("Reduce or discontinue Glipizide" / "Discontinue or
#: reduce Glipizide") score 0.67.
ACTION_WORDS = 5

#: Above this overlap between action heads, two interventions are the same
#: instruction. 0.6 sits in a measured gap — on the live plan, nine pairs
#: score at or above it and none fall between 0.5 and 0.6.
DUPLICATE_THRESHOLD = 0.6

#: Interventions at or above this count are more than a person can carry.
#: A number rather than a judgement, so it can be argued with.
BURDEN_LIMIT = 8


@dataclass(frozen=True)
class Veto:
    """A flag that forbids a kind of intervention.

    The deprescribing layer: a plan that has just recorded a drug as
    dangerous for this patient may not, three nodes later, propose starting
    more of it. That is not a hypothetical failure — it is what independent
    per-goal generation does when one goal says "reduce hypoglycaemia risk"
    and another says "improve glycaemic control".
    """

    rule_id: str
    forbids: tuple[str, ...]
    about: tuple[str, ...]
    reason: str


VETOES: tuple[Veto, ...] = (
    Veto(
        rule_id="sulfonylurea-in-older-adult",
        forbids=("start", "add", "increase", "titrate up", "uptitrate", "initiate"),
        about=("sulfonylurea", "glipizide", "glyburide", "glimepiride", "gliclazide"),
        reason="the plan flags this drug class as a hypoglycaemia risk for this patient",
    ),
    Veto(
        rule_id="deintensification-candidate",
        forbids=("tighten", "intensify", "lower the target", "reduce the target"),
        about=("glycaemic", "glycemic", "hba1c", "a1c", "glucose"),
        reason="the plan flags this patient for deintensification, not tighter control",
    ),
)


@dataclass
class ReconcileReport:
    """What reconciliation removed, and what it wants a human to see."""

    merged: list[str] = field(default_factory=list)
    vetoed: list[str] = field(default_factory=list)
    burden: int = 0
    burden_flagged: bool = False
    bare_goals: list[int] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        lines = [f"{len(self.merged)} merged, {len(self.vetoed)} vetoed, burden {self.burden}"]
        lines.extend(f"merged: {item}" for item in self.merged)
        lines.extend(f"vetoed: {item}" for item in self.vetoed)
        if self.burden_flagged:
            lines.append(f"burden {self.burden} at or above {BURDEN_LIMIT} — review before approval")
        for index in self.bare_goals:
            lines.append(f"goal {index} has no intervention of its own after merging")
        return lines


#: Where the instruction stops and the justification begins. The model
#: writes "<action>, given that <rationale>" — so everything after the first
#: clause break is explanation, and matching against it is matching against
#: the wrong half of the sentence.
_CLAUSE_BREAK = re.compile(r"[,;:(]|\s[-—]\s")


def action_clause(statement: str) -> str:
    """The instruction, without its rationale.

    This exists because matching the whole sentence is wrong twice over,
    and the second time was dangerous. See :func:`_violates`.
    """
    return _CLAUSE_BREAK.split(statement, maxsplit=1)[0]


def _action(statement: str) -> set[str]:
    """The leading content words — what the instruction actually asks."""
    words = [
        w
        for w in re.findall(r"[a-z]+", action_clause(statement).lower())
        if w not in _STOPWORDS and len(w) > 2
    ]
    return set(words[:ACTION_WORDS])


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _violates(intervention: InterventionDraft, veto: Veto) -> bool:
    """Does this intervention do the thing the flag forbids?

    The verb is matched against the ACTION only, never the rationale, and
    that distinction is not cosmetic. Run against real generated prose, the
    first version vetoed:

        "Discontinue or reduce Glipizide given the patient's age, CKD
        stage 3b (reduced renal clearance INCREASES hypoglycaemia risk)"

    — deleting the single most important intervention in the plan, because
    the forbidden word "increase" appeared in the clause explaining *why*
    the drug should be stopped. A safety mechanism that removes the safe
    action is worse than no safety mechanism.

    Only real prose exposed it: every statement in the unit tests was short
    enough to have no rationale to trip over.
    """
    full = intervention.statement.lower()
    if not any(subject in full for subject in veto.about):
        return False
    action = action_clause(full)
    return any(re.search(rf"\b{re.escape(verb)}", action) for verb in veto.forbids)


def reconcile(
    interventions: Sequence[InterventionDraft],
    flags: Sequence[RiskFlag],
    goal_count: int = 0,
) -> tuple[list[InterventionDraft], ReconcileReport]:
    """De-duplicate, veto, and count. Returns what survives."""
    report = ReconcileReport()

    active = {flag.rule_id for flag in flags}
    kept: list[InterventionDraft] = []
    heads: list[set[str]] = []

    for intervention in interventions:
        breached = next((v for v in VETOES if v.rule_id in active and _violates(intervention, v)), None)
        if breached is not None:
            report.vetoed.append(f"{intervention.statement[:70]} — {breached.reason}")
            continue

        head = _action(intervention.statement)
        twin = next(
            (i for i, existing in enumerate(heads) if _overlap(head, existing) >= DUPLICATE_THRESHOLD),
            None,
        )
        if twin is not None:
            report.merged.append(f"{intervention.statement[:70]} — already asked as #{twin + 1}")
            continue
        kept.append(intervention)
        heads.append(head)

    report.burden = len(kept)
    report.burden_flagged = report.burden >= BURDEN_LIMIT

    # A goal whose only interventions merged into another goal's is still
    # served — but a reader cannot see that, and this design's whole claim
    # is that every element traces. So it is surfaced, not silently allowed.
    served = {intervention.goal_index for intervention in kept}
    report.bare_goals = [index for index in range(goal_count) if index not in served]
    return kept, report
