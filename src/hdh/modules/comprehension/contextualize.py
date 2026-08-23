"""Stage 4: assertion — rules first, deterministic, transparent.

The section supplies the default (design §5); NegEx-lite local triggers
override it; shared triggers (§6) distribute across their section's
list. Precedence when several rules fire: negated > family_history >
historical > uncertain > section default. Every decision returns its
evidence string, so the stored mention explains itself. LLM adjudication
of rule disagreements is a later hook — the eval corpus decides whether
it is ever needed (master §3 stage 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hdh.modules.comprehension.contracts import (
    SECTION_DEFAULT_ASSERTION,
    Assertion,
    Extraction,
    Mention,
)

# trigger word → assertion, matched case-insensitively BEFORE the mention
# within the same sentence (precedence = this order)
#: Cues are matched on WORD BOUNDARIES, not as bare substrings — so
#: "no" cannot fire inside "note", and "son" cannot fire inside "reason
#: for visit" or a surname like "Wilson". The older table relied on
#: trailing-space hacks ("no ", "not ") which still let "not " fire
#: inside "cannot tolerate".
_PRE_TRIGGERS: tuple[tuple[str, Assertion], ...] = (
    ("no", Assertion.NEGATED),
    ("denies", Assertion.NEGATED),
    ("without", Assertion.NEGATED),
    ("negative for", Assertion.NEGATED),
    ("not", Assertion.NEGATED),
    ("family history of", Assertion.FAMILY_HISTORY),
    # every relative a note might name, not just the parents: our own
    # corpus writes "sister: breast cancer" and scored correctly only
    # because the subjective_family section default carried it.
    ("mother", Assertion.FAMILY_HISTORY),
    ("father", Assertion.FAMILY_HISTORY),
    ("parent", Assertion.FAMILY_HISTORY),
    ("sister", Assertion.FAMILY_HISTORY),
    ("brother", Assertion.FAMILY_HISTORY),
    ("sibling", Assertion.FAMILY_HISTORY),
    ("daughter", Assertion.FAMILY_HISTORY),
    ("son", Assertion.FAMILY_HISTORY),
    ("aunt", Assertion.FAMILY_HISTORY),
    ("uncle", Assertion.FAMILY_HISTORY),
    ("cousin", Assertion.FAMILY_HISTORY),
    ("niece", Assertion.FAMILY_HISTORY),
    ("nephew", Assertion.FAMILY_HISTORY),
    ("grandmother", Assertion.FAMILY_HISTORY),
    ("grandfather", Assertion.FAMILY_HISTORY),
    ("grandparent", Assertion.FAMILY_HISTORY),
    ("history of", Assertion.HISTORICAL),
    # The abbreviations are how notes actually write it. Spelling it out is
    # the exception, so without these the commonest form of "this is
    # background, not today" reads as a present-tense complaint.
    ("h/o", Assertion.HISTORICAL),
    ("hx of", Assertion.HISTORICAL),
    ("possible", Assertion.UNCERTAIN),
    ("probable", Assertion.UNCERTAIN),
    ("suspected", Assertion.UNCERTAIN),
    ("likely", Assertion.UNCERTAIN),
    ("consider", Assertion.UNCERTAIN),
)
_POST_TRIGGERS: tuple[tuple[str, Assertion], ...] = (
    ("resolved", Assertion.HISTORICAL),
    ("ruled out", Assertion.NEGATED),
)
_PRECEDENCE = (
    Assertion.NEGATED,
    Assertion.FAMILY_HISTORY,
    Assertion.HISTORICAL,
    Assertion.UNCERTAIN,
)
_PATTERNS: dict[str, re.Pattern[str]] = {}
_WINDOW = 60  # chars of same-sentence context searched around the mention


@dataclass(frozen=True)
class AssertionResult:
    """The finalized assertion with its evidence trail."""

    assertion: Assertion
    evidence: str  # "section default" | "trigger: 'denies'" | "shared trigger: 'No'"


def _re_compile_word(trigger: str):
    """Compile a whole-word matcher for one cue."""
    boundary = chr(92) + "b"  # a literal regex word boundary
    return re.compile(boundary + re.escape(trigger) + boundary)


def _word_pattern(trigger: str) -> re.Pattern[str]:
    """A cached word-boundary matcher for one cue."""
    return _PATTERNS.setdefault(trigger, _re_compile_word(trigger))


def _last_word_match(haystack: str, trigger: str) -> int:
    """Rightmost whole-word occurrence, or -1 (the backwards scan)."""
    matches = list(_word_pattern(trigger).finditer(haystack))
    return matches[-1].start() if matches else -1


def _first_word_match(haystack: str, trigger: str) -> int:
    """Leftmost whole-word occurrence, or -1 (the forwards scan)."""
    match = _word_pattern(trigger).search(haystack)
    return match.start() if match else -1


def _same_sentence(note: str, start: int, end: int) -> bool:
    return "." not in note[start:end]


def _inside_another_mention(extraction: Extraction, mention: Mention, cue_start: int) -> bool:
    """Is this cue part of some OTHER mention's own text?

    Diagnosis names contain negation words: "Type 2 diabetes mellitus
    **without** complications" sits in the history list, and a backwards
    scan finds that "without" and negates every condition listed after
    it. Measured on the corpus, this single false positive accounted for
    all 20 assertion mismatches — "Essential hypertension" and "COPD with
    acute exacerbation" were being recorded as things the patient does
    NOT have.

    A cue inside another mention's span is part of a name, not a
    negation of the text that follows it."""
    return any(
        other.span.start <= cue_start < other.span.end
        for other in extraction.mentions
        if other.id != mention.id
    )


def finalize_assertion(extraction: Extraction, mention: Mention) -> AssertionResult:
    """Section default + local/shared trigger overrides, by precedence."""
    note = extraction.note_text
    section = extraction.section_of(mention)
    default = SECTION_DEFAULT_ASSERTION.get(section.kind) or Assertion.PRESENT
    fired: list[tuple[Assertion, str]] = []

    window_start = max(section.span.start, mention.span.start - _WINDOW)
    before = note[window_start : mention.span.start].lower()
    for trigger, assertion in _PRE_TRIGGERS:
        position = _last_word_match(before, trigger)
        if position == -1 or not _same_sentence(before, position + len(trigger), len(before)):
            continue
        if _inside_another_mention(extraction, mention, window_start + position):
            continue  # part of a diagnosis name, not a cue
        fired.append((assertion, f"trigger: {trigger.strip()!r}"))
    window_end = min(section.span.end, mention.span.end + _WINDOW)
    after = note[mention.span.end : window_end].lower()
    for trigger, assertion in _POST_TRIGGERS:
        position = _first_word_match(after, trigger)
        if position == -1 or not _same_sentence(after, 0, position):
            continue
        if _inside_another_mention(extraction, mention, mention.span.end + position):
            continue  # part of a diagnosis name, not a cue
        fired.append((assertion, f"trigger: {trigger!r}"))

    for shared in extraction.shared_triggers:
        if (
            shared.section_id == mention.section_id
            and shared.span.start < mention.span.start
            and _same_sentence(note, shared.span.end, mention.span.start)
        ):
            fired.append((Assertion.NEGATED, f"shared trigger: {shared.text!r}"))

    for assertion in _PRECEDENCE:
        for candidate, evidence in fired:
            if candidate is assertion:
                return AssertionResult(assertion=assertion, evidence=evidence)
    return AssertionResult(assertion=default, evidence=f"section default ({section.kind.value})")
