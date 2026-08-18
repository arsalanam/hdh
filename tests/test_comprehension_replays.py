"""The replay corpus (design §14.2): every live LLM failure, frozen.

Each fixture in `fixtures/comprehension/replays/` is the raw extractor
output that once broke us, replayed through the *real* validator. The
standing rule this file enforces: **no live failure is fixed without its
replay landing here** — so the bug that cost three retries in a chat
session costs zero seconds in CI forever after.

Data-driven on purpose: a new failure is a new JSON file, never new test
code. Zero API calls.
"""

import json
from pathlib import Path

import pytest

from hdh.modules.comprehension.segment import segment
from hdh.modules.comprehension.validate import ExtractionError, build_extraction

REPLAYS = Path(__file__).parent / "fixtures" / "comprehension" / "replays"
CASES = sorted(REPLAYS.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_corpus_is_not_empty():
    """A silently-empty corpus would make every replay below vacuous."""
    assert CASES, f"no replay fixtures in {REPLAYS}"
    for path in CASES:
        case = _load(path)
        assert case["description"], f"{path.name}: a replay must say what it froze"
        assert case["expect"]["status"] in ("accepted", "rejected")


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_replay(path: Path):
    case = _load(path)
    note, expect = case["note"], case["expect"]

    if expect["status"] == "rejected":
        with pytest.raises(ExtractionError) as err:
            build_extraction(note, case["raw"], segment(note))
        joined = " · ".join(err.value.reasons)
        for fragment in expect["reasons"]:
            assert fragment in joined, f"{path.stem}: expected {fragment!r} in {joined!r}"
        return

    extraction = build_extraction(note, case["raw"], segment(note))
    assert len(extraction.mentions) == expect["mentions"]
    assert len(extraction.relations) == expect.get("relations", 0)
    for text in expect.get("texts", ()):
        assert any(m.text == text for m in extraction.mentions), f"{path.stem}: missing {text!r}"
    if "relation_target" in expect:
        target = extraction.mentions[extraction.relations[0].target_id]
        assert target.text == expect["relation_target"]

    # the invariant every accepted replay must still satisfy
    for mention in extraction.mentions:
        assert note[mention.span.start : mention.span.end] == mention.text
        for attribute in mention.attributes:
            assert note[attribute.span.start : attribute.span.end] == attribute.text


def test_realignment_verifies_rather_than_guesses():
    """The realignment must only ever choose a VERBATIM occurrence, and
    must leave the mention alone when no coherent occurrence exists —
    otherwise a rejection would be silently converted into a wrong span."""
    note = (
        "SOAP NOTE\nProvider: Dr. Test\n\nS: Reports pain.\n\n"
        "O: BP 138/82, pain 2/10.\n\nA: Osteoarthritis.\n\nP: Continue.\n"
    )
    from hdh.modules.comprehension.validate import _realign_to_attributes

    sections = segment(note)
    item = {
        "type": "lab_vital",
        "text": "pain",
        "occurrence": 1,
        "attributes": [{"kind": "value", "text": "2/10", "occurrence": 1}],
    }
    original = build_extraction(note, {"mentions": [item]}, sections).mentions[0]
    assert note[original.span.start : original.span.end] == "pain"  # still verbatim
    assert original.attributes and original.attributes[0].text == "2/10"

    # no attributes → nothing to realign against, span is left untouched
    plain = {"type": "problem", "text": "pain", "occurrence": 1, "attributes": []}
    from hdh.modules.comprehension.validate import _locate

    reasons: list[str] = []
    span = _locate(note, plain, "m", reasons)
    assert _realign_to_attributes(note, sections, plain, span) == span


def test_a_negation_word_inside_a_diagnosis_name_is_not_a_negation():
    """ "Type 2 diabetes mellitus WITHOUT complications" sits in the history
    list, and a backwards NegEx scan found that "without" and negated
    every condition listed after it. On the real corpus this single false
    positive accounted for ALL 20 assertion mismatches — hypertension and
    COPD were being recorded as things the patient does not have."""
    from hdh.modules.comprehension.contextualize import finalize_assertion
    from hdh.modules.comprehension.contracts import Assertion

    note = (
        "SOAP NOTE\nProvider: Dr. Test\n\n"
        "S: Reports fatigue. History of: Type 2 diabetes mellitus without complications, "
        "Essential hypertension, COPD with acute exacerbation.\n\n"
        "O: BP 138/82 mmHg.\n\nA: Fatigue.\n\nP: Continue.\n"
    )
    raw = {
        "mentions": [
            {
                "type": "problem",
                "text": "Type 2 diabetes mellitus without complications",
                "occurrence": 1,
                "attributes": [],
            },
            {"type": "problem", "text": "Essential hypertension", "occurrence": 1, "attributes": []},
            {"type": "problem", "text": "COPD with acute exacerbation", "occurrence": 1, "attributes": []},
        ]
    }
    extraction = build_extraction(note, raw, segment(note))
    by_text = {m.text: m for m in extraction.mentions}

    for name in ("Essential hypertension", "COPD with acute exacerbation"):
        result = finalize_assertion(extraction, by_text[name])
        assert result.assertion is Assertion.HISTORICAL, f"{name}: {result.assertion} ({result.evidence})"

    # the diabetes mention itself is history too — its own name must not
    # negate it either
    diabetes = finalize_assertion(extraction, by_text["Type 2 diabetes mellitus without complications"])
    assert diabetes.assertion is Assertion.HISTORICAL, diabetes.evidence


def test_a_real_negation_still_negates():
    """The fix must not blunt NegEx: a cue OUTSIDE every mention span
    still fires."""
    from hdh.modules.comprehension.contextualize import finalize_assertion
    from hdh.modules.comprehension.contracts import Assertion

    note = (
        "SOAP NOTE\nProvider: Dr. Test\n\nS: Patient denies chest pain.\n\n"
        "O: BP 138/82 mmHg.\n\nA: Fatigue.\n\nP: Continue.\n"
    )
    raw = {"mentions": [{"type": "problem", "text": "chest pain", "occurrence": 1, "attributes": []}]}
    extraction = build_extraction(note, raw, segment(note))
    result = finalize_assertion(extraction, extraction.mentions[0])
    assert result.assertion is Assertion.NEGATED, result.evidence
    assert "denies" in result.evidence
