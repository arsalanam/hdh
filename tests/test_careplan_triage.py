"""Care plan, node 2b: deciding what the plan is about (#104).

Node 3 used to retrieve once for the whole patient — the entire chart as a
single query, six candidates back — which cannot span a long problem list.
The measurement that prompted this: on a ten-problem chart the blended
query returned six chunks covering five conditions, every score between
0.0065 and 0.0112, and missed the one condition recorded as uncontrolled.
The same corpus asked one topic at a time returned the right document at
rank 1 fourteen times out of fourteen.

Nothing here calls a model, so all of it runs offline.
"""

from __future__ import annotations

from hdh.modules.careplan.context import CarePlanContext, ProblemView
from hdh.modules.careplan.stratify import RiskFlag
from hdh.modules.careplan.triage import (
    FLAG,
    PROBLEM_LIMIT,
    STABLE,
    UNCONTROLLED,
    deferral_lines,
    triage,
)

#: The chart that exposed the blended-query failure, verbatim in shape.
PROBLEMS = [
    ("I10", "Essential hypertension", True),
    ("E11.9", "Type 2 diabetes mellitus", True),
    ("E78.5", "Hyperlipidemia, unspecified", False),
    ("M19.90", "Unspecified osteoarthritis, unspecified site", True),
    ("J44.1", "COPD with acute exacerbation", True),
    ("D50.9", "Iron deficiency anemia, unspecified", True),
    ("I48.91", "Unspecified atrial fibrillation", True),
    ("I50.32", "Chronic diastolic heart failure", True),
    ("N18.4", "Chronic kidney disease, stage 3b", True),
    ("Z86.73", "Personal history of TIA and cerebral infarction", True),
]


def _context(problems=None) -> CarePlanContext:
    rows = PROBLEMS if problems is None else problems
    return CarePlanContext(
        mrn="TEST01",
        age=84,
        sex="MALE",
        problems=tuple(ProblemView(code, text, controlled, None) for code, text, controlled in rows),
    )


def _flag(rule_id: str, statement: str, basis: str) -> RiskFlag:
    return RiskFlag(rule_id, "disease_control", statement, basis, "med_safety/x")


UNCONTROLLED_FLAG = _flag(
    "uncontrolled-chronic",
    "Chronic condition recorded as not controlled",
    "Hyperlipidemia, unspecified (E78.5)",
)
POLYPHARMACY_FLAG = _flag("polypharmacy", "Polypharmacy worth reviewing", "8 distinct active medications")


# ── the dedupe, and the bug that made it dangerous ───────────────────────


def test_a_problem_a_flag_already_covers_is_not_topiced_twice():
    """The `uncontrolled-chronic` rule's basis names the very problems that
    would otherwise become topics of their own, and two concerns about one
    condition is how a plan starts saying the same thing twice."""
    selected, deferred = triage(_context(), [UNCONTROLLED_FLAG])
    keys = {topic.key for topic in selected + deferred}
    assert "problem:E78.5" not in keys
    assert "flag:uncontrolled-chronic" in keys


def test_dedupe_does_not_delete_a_problem_over_a_shared_filler_word():
    """The regression, and the most important test in this file.

    The first version matched flag text against problem wording with a
    lexical OR over significant words. On this exact chart it dropped four
    conditions:

    - osteoarthritis and atrial fibrillation, because both descriptions
      contain "unspecified", which also appears in "Hyperlipidemia,
      unspecified"
    - heart failure and chronic kidney disease, on the word "chronic",
      which appears in "Chronic condition recorded as not controlled"

    Four conditions silently removed from a plan by two of the least
    meaningful words in the sentence. A lexical OR is right for asking
    whether a plan discusses something at all, where a false positive costs
    nothing; it is wrong here, where a false positive deletes a topic.
    """
    selected, deferred = triage(_context(), [UNCONTROLLED_FLAG, POLYPHARMACY_FLAG])
    keys = {topic.key for topic in selected + deferred}
    for code in ("M19.90", "I48.91", "I50.32", "N18.4"):
        assert f"problem:{code}" in keys, f"{code} was dropped by the dedupe"


def test_a_flag_whose_basis_names_no_code_removes_nothing():
    """`polypharmacy` says "8 distinct active medications". It covers no
    particular problem and must not appear to."""
    selected, deferred = triage(_context(), [POLYPHARMACY_FLAG])
    problems = [t for t in selected + deferred if t.key.startswith("problem:")]
    assert len(problems) == len(PROBLEMS)


# ── ordering and the cap ─────────────────────────────────────────────────


def test_flags_come_first_and_are_never_deferred():
    """Flags are deterministic safety findings the rules already justified,
    they are bounded by the size of the rule set, and the safety dimension
    grades whether the plan answered them. Deferring one would defer the
    part of the plan most likely to matter."""
    flags = [POLYPHARMACY_FLAG, _flag("other", "Another finding", "because")]
    selected, deferred = triage(_context(), flags, limit=1)
    assert [t.priority for t in selected[:2]] == [FLAG, FLAG]
    assert not any(t.is_flag for t in deferred)


def test_an_uncontrolled_problem_outranks_a_stable_one():
    problems = [
        ("I10", "Essential hypertension", True),
        ("E78.5", "Hyperlipidemia, unspecified", False),
    ]
    selected, _deferred = triage(_context(problems), [])
    assert selected[0].key == "problem:E78.5"
    assert selected[0].priority == UNCONTROLLED
    assert selected[1].priority == STABLE


def test_the_cap_applies_to_problems_and_the_rest_are_deferred():
    selected, deferred = triage(_context(), [], limit=4)
    assert len(selected) == 4
    assert len(deferred) == len(PROBLEMS) - 4


def test_nothing_is_lost_between_selected_and_deferred():
    """A problem that is neither addressed nor recorded as deferred has
    simply vanished, which is the failure this whole node exists to stop."""
    selected, deferred = triage(_context(), [POLYPHARMACY_FLAG])
    keys = {t.key for t in selected + deferred if t.key.startswith("problem:")}
    assert keys == {f"problem:{code}" for code, _text, _ctl in PROBLEMS}


def test_triage_is_reproducible_for_the_same_chart():
    """Same chart, same plan shape — a run that triaged differently each
    time would make every before-and-after measurement meaningless."""
    first = [t.key for t in triage(_context(), [UNCONTROLLED_FLAG])[0]]
    second = [t.key for t in triage(_context(), [UNCONTROLLED_FLAG])[0]]
    assert first == second


def test_the_default_limit_is_a_named_constant():
    assert PROBLEM_LIMIT > 0
    selected, _deferred = triage(_context(), [])
    assert len(selected) == min(PROBLEM_LIMIT, len(PROBLEMS))


# ── queries and deferral text ────────────────────────────────────────────


def test_each_topic_queries_one_subject():
    """The whole point. A topic query that carried the patient's other nine
    problems would rebuild the blended query this node exists to replace."""
    selected, _deferred = triage(_context(), [])
    for topic in selected:
        assert topic.query.count(",") <= 1, f"{topic.key} looks blended: {topic.query!r}"
        others = [text for _code, text, _ctl in PROBLEMS if text != topic.label]
        assert not any(other in topic.query for other in others)


def test_a_topic_says_why_it_was_chosen():
    selected, _deferred = triage(_context(), [UNCONTROLLED_FLAG])
    assert all(topic.basis for topic in selected)
    flag_topic = next(t for t in selected if t.is_flag)
    assert "uncontrolled-chronic" in flag_topic.basis


def test_deferral_lines_name_the_problem_and_say_it_was_not_addressed():
    _selected, deferred = triage(_context(), [], limit=2)
    lines = deferral_lines(deferred)
    assert len(lines) == len(deferred)
    assert all("not addressed in this plan" in line for line in lines)
    assert any("Chronic diastolic heart failure" in line for line in lines)


def test_a_chart_with_no_problems_defers_nothing_and_selects_nothing():
    selected, deferred = triage(_context([]), [])
    assert selected == [] and deferred == []
