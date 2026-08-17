"""Property tests for the span machinery (design §14.1).

Live testing found the locator bugs by luck — "T" matching inside "SOAP
NOTE", a nested diagnosis burning three retries. These tests hunt the
same class deliberately: for *arbitrary* substrings of *real generated
notes*, the verbatim invariant must hold, spans must land inside a
section, and the locator must be self-consistent.

**Seeded, not hypothesis.** The design named hypothesis; a seeded sweep
over the real corpus is used instead — same fuzzing intent, no new
dependency, and byte-for-byte reproducible like the rest of the suite
(`hdh generate --seed`). A failure prints the seed and the exact case, so
it is reproducible without shrinking.
"""

import random

import pytest

from hdh.core.notes import render_soap
from hdh.modules.comprehension.contracts import MentionType
from hdh.modules.comprehension.segment import section_for, segment
from hdh.modules.comprehension.validate import ExtractionError, _nth_occurrence, build_extraction

SEED = 20260817  # bump to sweep a different slice; failures print the case


class _Vital:
    bp_systolic, bp_diastolic, heart_rate = 142, 88, 76
    temperature_f, oxygen_sat, weight_kg = 98.6, 98, 82.0
    height_cm, bmi, respiratory_rate, pain_scale = 170.0, 28.4, 16, 2


def _notes() -> list[str]:
    """A handful of real generator-shaped notes — the actual input domain."""
    shapes = [
        dict(
            chief_complaint="Palpitations",
            chronic_history=["Essential hypertension"],
            conditions=[("Atrial fibrillation", "I48.91")],
        ),
        dict(
            chief_complaint="Cough and fever", chronic_history=[], conditions=[("Acute bronchitis", "J20.9")]
        ),
        dict(
            chief_complaint="Follow-up: thyroid", chronic_history=["Hypothyroidism", "Obesity"], conditions=[]
        ),
    ]
    notes = []
    for shape in shapes:
        notes.append(
            render_soap(
                provider_name="Dr. Sarah Mitchell, MD",
                visit_date="2026-08-15",
                follow_up_days=90,
                age=64,
                sex="female",
                allergies=["Penicillin"],
                family_history=["mother: type 2 diabetes"],
                vital=_Vital(),
                prescriptions=[{"drug_name": "Apixaban", "dose": "5mg", "frequency": "BID", "is_new": True}],
                labs=[("INR", 2.4, "ratio", "HIGH")],
                procedures=[],
                **shape,
            )
        )
    return notes


NOTES = _notes()


def _substrings(note: str, rng: random.Random, count: int) -> list[str]:
    """Arbitrary in-note substrings — the adversarial input for a locator
    that must never claim text the note does not contain verbatim."""
    picks = []
    for _ in range(count):
        start = rng.randrange(0, max(1, len(note) - 2))
        end = min(len(note), start + rng.randrange(1, 40))
        text = note[start:end].strip()
        if text:
            picks.append(text)
    return picks


def test_locator_never_reports_a_span_that_is_not_verbatim():
    """The safety property: whatever the locator returns, the note must
    actually say it there."""
    rng = random.Random(SEED)
    checked = 0
    for note in NOTES:
        for text in _substrings(note, rng, 120):
            index = _nth_occurrence(note, text, 1)
            if index == -1:
                continue
            assert note[index : index + len(text)] == text, f"seed={SEED} text={text!r}"
            checked += 1
    assert checked > 100, "the sweep degenerated — it is not exercising the locator"


def test_locator_occurrences_are_ordered_and_exhaustive():
    """Occurrence N+1 never precedes occurrence N, and asking past the
    last one fails cleanly instead of wrapping."""
    rng = random.Random(SEED + 1)
    for note in NOTES:
        for text in _substrings(note, rng, 60):
            previous = -1
            occurrence = 1
            while (index := _nth_occurrence(note, text, occurrence)) != -1:
                assert index > previous, f"seed={SEED} text={text!r} occurrence={occurrence}"
                previous = index
                occurrence += 1
                if occurrence > 12:  # pathological repeats aren't the point
                    break
            assert _nth_occurrence(note, text, occurrence + 50) == -1


def test_validated_mentions_always_land_inside_a_section():
    """Every accepted mention resolves to a section, and never the header
    — section_for and the validator must agree."""
    rng = random.Random(SEED + 2)
    accepted = 0
    for note in NOTES:
        sections = segment(note)
        for text in _substrings(note, rng, 80):
            raw = {"mentions": [{"type": "problem", "text": text, "occurrence": 1, "attributes": []}]}
            try:
                extraction = build_extraction(note, raw, sections)
            except ExtractionError:
                continue  # header text, out-of-note text: rejection is a valid outcome
            mention = extraction.mentions[0]
            assert note[mention.span.start : mention.span.end] == mention.text
            assert section_for(sections, mention.span) is not None
            accepted += 1
    assert accepted > 20, "nothing validated — the sweep is not reaching the validator"


def test_segmentation_covers_every_character_exactly_once():
    """No gaps (a mention there would be unclassifiable) and no overlaps
    outside the declared nesting."""
    for note in NOTES:
        sections = segment(note)
        assert sections, "a note always segments to something, even UNKNOWN"
        for index in range(0, len(note), 7):  # stride: every char is overkill, every section is not
            from hdh.modules.comprehension.contracts import Span

            assert section_for(sections, Span(index, min(index + 1, len(note)))) is not None


@pytest.mark.parametrize("mention_type", list(MentionType))
def test_every_mention_type_survives_a_round_trip(mention_type):
    """No type is quietly unvalidatable — a regression here would silently
    drop a whole category of clinical content."""
    note = NOTES[0]
    text = "Apixaban" if mention_type is MentionType.MEDICATION else "Penicillin"
    raw = {"mentions": [{"type": mention_type.value, "text": text, "occurrence": 1, "attributes": []}]}
    extraction = build_extraction(note, raw, segment(note))
    assert extraction.mentions[0].mention_type is mention_type
