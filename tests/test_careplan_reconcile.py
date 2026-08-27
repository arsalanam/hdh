"""Care plan, milestone 2c: node 6, reconciliation.

Design §7 calls this "the step where naive systems fail", and the first
live run agreed: 25 interventions for one patient, five of them variations
on *"establish a monitoring arrangement"*.

Two of the tests here exist because of bugs that **only real generated
prose exposed**. Both had the same cause — the model writes a short action
followed by a long rationale, and matching against the whole sentence
matches the wrong half. Every statement in an earlier draft of these tests
was short enough to have no rationale to trip over.
"""

from __future__ import annotations

from hdh.modules.careplan.generate import InterventionDraft
from hdh.modules.careplan.reconcile import (
    BURDEN_LIMIT,
    DUPLICATE_THRESHOLD,
    action_clause,
    reconcile,
)
from hdh.modules.careplan.stratify import RiskFlag

#: Verbatim from the live run — long enough to have a rationale, which is
#: the whole point. A short paraphrase would not reproduce the bug.
REAL_DEPRESCRIBE = (
    "Discontinue or reduce Glipizide given the patient's age, CKD stage 3b "
    "(reduced renal clearance increases hypoglycaemia risk), blunted adrenergic "
    "warning symptoms, and the fact that he lives alone meaning any unwitnessed "
    "hypoglycaemic episode carries heightened danger."
)


def _iv(statement: str, goal_index: int = 0, kind: str = "medication") -> InterventionDraft:
    return InterventionDraft(statement, goal_index, kind, "prescriber", ("med_safety/x",))


def _flags(*rule_ids: str) -> list[RiskFlag]:
    return [RiskFlag(r, "medication_safety", "s", "b", "med_safety/x") for r in rule_ids]


# ── the veto, and the bug that made it dangerous ─────────────────────────


def test_a_veto_stops_an_intervention_that_contradicts_a_flag():
    """A plan that has just flagged a drug as dangerous may not, three
    nodes later, propose more of it. That is not hypothetical — it is what
    independent per-goal generation does when one goal says "reduce
    hypoglycaemia risk" and another says "improve glycaemic control"."""
    kept, report = reconcile(
        [_iv("Increase the glipizide dose to improve glycaemic control")],
        _flags("sulfonylurea-in-older-adult"),
    )
    assert kept == []
    assert report.vetoed and "hypoglycaemia risk" in report.vetoed[0]


def test_a_veto_does_not_fire_on_a_forbidden_word_in_the_rationale():
    """The regression, and the most important test in this file.

    The first version matched forbidden verbs anywhere in the sentence, so
    it vetoed the statement below — deleting the single most important
    intervention in the plan, because "increases" appears in the clause
    explaining *why* the drug should be stopped.

    A safety mechanism that removes the safe action is worse than no safety
    mechanism at all.
    """
    kept, report = reconcile([_iv(REAL_DEPRESCRIBE)], _flags("sulfonylurea-in-older-adult"))
    assert len(kept) == 1, f"the deprescribing action was vetoed: {report.vetoed}"
    assert not report.vetoed


def test_the_action_clause_stops_where_the_justification_starts():
    assert action_clause(REAL_DEPRESCRIBE) == "Discontinue or reduce Glipizide given the patient's age"
    assert action_clause("Do a thing — because of reasons") == "Do a thing"
    assert action_clause("Do a thing (for reasons)") == "Do a thing "
    assert action_clause("No clause break here") == "No clause break here"


def test_a_veto_only_applies_when_its_flag_actually_fired():
    """Vetoes are consequences of findings, not standing prohibitions. If
    the patient was never flagged, starting a sulfonylurea is ordinary
    prescribing and not this system's business."""
    kept, report = reconcile([_iv("Start glipizide 5 mg daily")], _flags("polypharmacy"))
    assert len(kept) == 1
    assert not report.vetoed


def test_a_veto_needs_both_the_verb_and_the_subject():
    """ "Increase the frequency of foot checks" is not a sulfonylurea
    violation, however forbidden the verb is in another context."""
    kept, _ = reconcile([_iv("Increase the frequency of foot checks")], _flags("sulfonylurea-in-older-adult"))
    assert len(kept) == 1


# ── de-duplication ───────────────────────────────────────────────────────


def test_the_same_action_asked_twice_is_asked_once():
    kept, report = reconcile(
        [
            _iv("Establish a monitoring arrangement that alerts a nominated contact", 0),
            _iv("Establish a monitoring arrangement that alerts a family member", 1),
        ],
        [],
        goal_count=2,
    )
    assert len(kept) == 1
    assert report.merged and "already asked" in report.merged[0]


def test_a_paraphrase_of_the_same_action_also_merges():
    """The live pair that motivated comparing actions rather than
    sentences: whole-statement overlap put these at 0.474, under any
    threshold that would not also merge unrelated items."""
    kept, _ = reconcile(
        [
            _iv("Reduce or discontinue Glipizide, given that the patient lives alone", 0),
            _iv("Discontinue or reduce Glipizide given the patient's age and renal function", 1),
        ],
        [],
        goal_count=2,
    )
    assert len(kept) == 1


def test_genuinely_different_actions_are_both_kept():
    """Under-merging is the safer error. A reviewer can strike a duplicate;
    they cannot recover something the system deleted."""
    kept, _ = reconcile(
        [
            _iv("Review whether glipizide remains appropriate", 0),
            _iv("Educate the patient on recognising hypoglycaemia", 0, "education"),
            _iv("Refer to the community diabetes nurse", 0, "referral"),
        ],
        [],
    )
    assert len(kept) == 3


def test_merging_is_recorded_with_what_it_merged_into():
    kept, report = reconcile(
        [
            _iv("Establish a monitoring arrangement for hypoglycaemia", 0),
            _iv("Establish a monitoring arrangement for glucose", 1),
        ],
        [],
        goal_count=2,
    )
    assert len(kept) == 1
    assert "#1" in report.merged[0]


# ── burden, and what it refuses to do about it ───────────────────────────


def test_burden_is_counted_and_flagged_but_never_truncated():
    """§7 asks reconciliation to *flag* excessive burden for human
    attention. Cutting the list to fit would be the system making a
    clinical decision about which care to drop, which is exactly the
    decision it is least qualified to make."""
    # Genuinely distinct actions. An earlier version of this test used
    # "Do distinct thing number {i}", which differs only by a digit — and
    # digits are not content words, so all twelve shared an action head and
    # correctly merged into one. The fixture was wrong, not the code.
    actions = [
        "Review the current glucose-lowering regimen",
        "Educate the patient on hypoglycaemia recognition",
        "Refer to the community diabetes service",
        "Arrange a falls assessment",
        "Order renal function bloods",
        "Simplify the dosing schedule into blister packaging",
        "Book a medication review with the pharmacist",
        "Check footwear and perform a foot examination",
        "Discuss driving safety implications",
        "Provide written emergency contact instructions",
        "Assess cognition before changing the regimen",
        "Schedule a follow-up appointment in three months",
    ]
    assert len(actions) >= BURDEN_LIMIT + 1
    many = [_iv(action, 0) for action in actions]
    kept, report = reconcile(many, [])
    assert len(kept) == len(many), "reconciliation must not silently truncate"
    assert report.burden == len(many)
    assert report.burden_flagged
    assert any("review before approval" in line for line in report.as_lines())


def test_a_small_plan_is_not_flagged():
    kept, report = reconcile([_iv("Do one thing", 0), _iv("Do another separate thing", 0)], [])
    assert len(kept) == 2
    assert not report.burden_flagged


def test_a_goal_left_with_no_intervention_is_surfaced():
    """A goal whose only intervention merged into another goal's is still
    served — but a reader cannot see that, and this design's whole claim is
    that every element traces. So it is reported, not silently allowed."""
    _kept, report = reconcile(
        [
            _iv("Establish a monitoring arrangement for hypoglycaemia", 0),
            _iv("Establish a monitoring arrangement for glucose", 1),
        ],
        [],
        goal_count=2,
    )
    assert report.bare_goals == [1]
    assert any("no intervention of its own" in line for line in report.as_lines())


def test_nothing_in_gives_nothing_out_without_complaint():
    kept, report = reconcile([], [], goal_count=0)
    assert kept == []
    assert report.burden == 0
    assert not report.burden_flagged


def test_the_threshold_is_a_named_constant_not_a_literal():
    """It was set by measurement on real output, and the next person to
    change it should have to find the reasoning."""
    assert 0.0 < DUPLICATE_THRESHOLD < 1.0
