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

from dataclasses import dataclass

from hdh.modules.comprehension.contracts import (
    SECTION_DEFAULT_ASSERTION,
    Assertion,
    Extraction,
    Mention,
)

# trigger word → assertion, matched case-insensitively BEFORE the mention
# within the same sentence (precedence = this order)
_PRE_TRIGGERS: tuple[tuple[str, Assertion], ...] = (
    ("no ", Assertion.NEGATED),
    ("denies", Assertion.NEGATED),
    ("without", Assertion.NEGATED),
    ("negative for", Assertion.NEGATED),
    ("not ", Assertion.NEGATED),
    ("family history of", Assertion.FAMILY_HISTORY),
    ("mother", Assertion.FAMILY_HISTORY),
    ("father", Assertion.FAMILY_HISTORY),
    ("history of", Assertion.HISTORICAL),
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
_WINDOW = 60  # chars of same-sentence context searched around the mention


@dataclass(frozen=True)
class AssertionResult:
    """The finalized assertion with its evidence trail."""

    assertion: Assertion
    evidence: str  # "section default" | "trigger: 'denies'" | "shared trigger: 'No'"


def _same_sentence(note: str, start: int, end: int) -> bool:
    return "." not in note[start:end]


def finalize_assertion(extraction: Extraction, mention: Mention) -> AssertionResult:
    """Section default + local/shared trigger overrides, by precedence."""
    note = extraction.note_text
    section = extraction.section_of(mention)
    default = SECTION_DEFAULT_ASSERTION.get(section.kind) or Assertion.PRESENT
    fired: list[tuple[Assertion, str]] = []

    window_start = max(section.span.start, mention.span.start - _WINDOW)
    before = note[window_start : mention.span.start].lower()
    for trigger, assertion in _PRE_TRIGGERS:
        position = before.rfind(trigger)
        if position != -1 and _same_sentence(before, position + len(trigger), len(before)):
            fired.append((assertion, f"trigger: {trigger.strip()!r}"))
    window_end = min(section.span.end, mention.span.end + _WINDOW)
    after = note[mention.span.end : window_end].lower()
    for trigger, assertion in _POST_TRIGGERS:
        position = after.find(trigger)
        if position != -1 and _same_sentence(after, 0, position):
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
