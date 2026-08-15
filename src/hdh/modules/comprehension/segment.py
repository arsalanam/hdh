"""Stage 1: deterministic segmentation (design §5) — regex, never an LLM.

Matches the family-medicine pack's note shape (core ``render_soap``) and
tolerates polished variants: whatever cannot be classified becomes ONE
``UNKNOWN`` section spanning the unmatched text — never a silent skip.
Sub-sections of S (history / family / allergies) nest inside it; a
mention is assigned to the most specific section containing it.
"""

from __future__ import annotations

import re

from hdh.modules.comprehension.contracts import Section, SectionKind, Span

_TOP = (
    (SectionKind.HEADER, re.compile(r"^SOAP NOTE\b.*$", re.MULTILINE)),
    (SectionKind.SUBJECTIVE, re.compile(r"^S:\s?.*(?:\n(?![OAP]:).*)*", re.MULTILINE)),
    (SectionKind.OBJECTIVE, re.compile(r"^O:\s?.*(?:\n(?![SAP]:).*)*", re.MULTILINE)),
    (SectionKind.ASSESSMENT, re.compile(r"^A:\s?.*(?:\n(?![SOP]:).*)*", re.MULTILINE)),
    (SectionKind.PLAN, re.compile(r"^P:\s?.*(?:\n(?![SOA]:).*)*", re.MULTILINE)),
)

# Sub-sentences within S — the marker through its closing period.
_SUBJECTIVE_SUB = (
    (SectionKind.SUBJECTIVE_ALLERGY, re.compile(r"Known allergies:[^.]*\.")),
    (SectionKind.SUBJECTIVE_HISTORY, re.compile(r"History of:[^.]*\.")),
    (SectionKind.SUBJECTIVE_FAMILY, re.compile(r"Family history:[^.]*\.")),
)


def segment(note_text: str) -> tuple[Section, ...]:
    """Split a note into sections with spans; UNKNOWN fallback, no gaps
    in coverage of mention-bearing text."""
    sections: list[Section] = []
    covered: list[Span] = []

    def add(kind: SectionKind, start: int, end: int) -> None:
        sections.append(Section(id=len(sections), kind=kind, span=Span(start, end)))

    for kind, pattern in _TOP:
        match = pattern.search(note_text)
        if match:
            add(kind, match.start(), match.end())
            covered.append(Span(match.start(), match.end()))
            if kind is SectionKind.SUBJECTIVE:
                for sub_kind, sub_pattern in _SUBJECTIVE_SUB:
                    sub = sub_pattern.search(note_text, match.start(), match.end())
                    if sub:
                        add(sub_kind, sub.start(), sub.end())

    if not covered:  # unrecognized shape: one UNKNOWN section, whole note
        add(SectionKind.UNKNOWN, 0, len(note_text))
        return tuple(sections)

    # any substantial text outside the top-level sections → UNKNOWN
    covered.sort()
    cursor = 0
    for span in covered:
        gap = note_text[cursor : span.start].strip()
        if len(gap) > 2:
            add(SectionKind.UNKNOWN, cursor, span.start)
        cursor = max(cursor, span.end)
    if len(note_text[cursor:].strip()) > 2:
        add(SectionKind.UNKNOWN, cursor, len(note_text))
    return tuple(sections)


def section_for(sections: tuple[Section, ...], span: Span) -> Section | None:
    """The MOST SPECIFIC (smallest) section fully containing a span."""
    containing = [s for s in sections if s.span.start <= span.start and span.end <= s.span.end]
    if not containing:
        return None
    return min(containing, key=lambda s: s.span.end - s.span.start)
