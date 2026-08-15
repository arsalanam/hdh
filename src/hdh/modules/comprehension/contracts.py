"""The extraction contracts (design comprehension-extraction-schema.md §1–§6).

Frozen dataclasses and CLOSED enums — an extractor wanting a new kind is
a schema change reviewed here, never a free string. What a mention
deliberately does not carry at extraction time: code (stage 3's job),
assertion (stage 4's, seeded by the section default), confidence
(stages 3–5).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import NamedTuple


class Span(NamedTuple):
    """Character offsets into the ORIGINAL note text (end exclusive)."""

    start: int
    end: int


class MentionType(enum.Enum):
    """The five clinical entity kinds extraction may emit (§1; §11 Q3
    decided ALLERGY as the fifth type)."""

    PROBLEM = "problem"
    MEDICATION = "medication"
    LAB_VITAL = "lab_vital"
    PROCEDURE = "procedure"
    ALLERGY = "allergy"


class AttributeKind(enum.Enum):
    """Typed sub-spans inside a mention (§3) — the closed table."""

    DOSE = "dose"
    FREQUENCY = "frequency"
    DURATION = "duration"
    ROUTE = "route"
    VALUE = "value"
    UNIT = "unit"
    INTERPRETATION = "interpretation"
    LATERALITY = "laterality"
    BODY_SITE = "body_site"
    SEVERITY = "severity"
    STAGE = "stage"
    STATUS_WORD = "status_word"
    REACTION = "reaction"
    CONTROL = "control"


# Which attribute kinds are legal on which mention types (§3) — the
# validator's table, stated once.
ATTRIBUTE_LEGALITY: dict[AttributeKind, frozenset[MentionType]] = {
    AttributeKind.DOSE: frozenset({MentionType.MEDICATION}),
    AttributeKind.FREQUENCY: frozenset({MentionType.MEDICATION}),
    AttributeKind.DURATION: frozenset({MentionType.MEDICATION}),
    AttributeKind.ROUTE: frozenset({MentionType.MEDICATION}),
    AttributeKind.VALUE: frozenset({MentionType.LAB_VITAL}),
    AttributeKind.UNIT: frozenset({MentionType.LAB_VITAL}),
    AttributeKind.INTERPRETATION: frozenset({MentionType.LAB_VITAL}),
    AttributeKind.LATERALITY: frozenset({MentionType.PROBLEM, MentionType.PROCEDURE}),
    AttributeKind.BODY_SITE: frozenset({MentionType.PROBLEM, MentionType.PROCEDURE}),
    AttributeKind.SEVERITY: frozenset({MentionType.PROBLEM, MentionType.ALLERGY}),
    AttributeKind.STAGE: frozenset({MentionType.PROBLEM}),
    AttributeKind.STATUS_WORD: frozenset({MentionType.MEDICATION, MentionType.PROCEDURE}),
    AttributeKind.REACTION: frozenset({MentionType.ALLERGY}),
    # disease-control status ('well controlled', 'worsening') — maps onto
    # the chart's Condition.controlled flag in the stage-6 applier
    AttributeKind.CONTROL: frozenset({MentionType.PROBLEM}),
}


class RelationKind(enum.Enum):
    """Mention-to-mention relations (§4.1). TREATS is v1; MEASURES and
    REVEALS are RESERVED — schema-known, extractor-silent until their
    consumers (care-gaps, the grounding validator) land."""

    TREATS = "treats"
    MEASURES = "measures"  # reserved v1.5
    REVEALS = "reveals"  # reserved v1.5


EMITTABLE_RELATIONS = frozenset({RelationKind.TREATS})

# source types → target types per relation kind (§4)
RELATION_RULES: dict[RelationKind, tuple[frozenset[MentionType], frozenset[MentionType]]] = {
    RelationKind.TREATS: (
        frozenset({MentionType.MEDICATION, MentionType.PROCEDURE}),
        frozenset({MentionType.PROBLEM, MentionType.LAB_VITAL}),
    ),
    RelationKind.MEASURES: (
        frozenset({MentionType.LAB_VITAL}),
        frozenset({MentionType.PROBLEM}),
    ),
    RelationKind.REVEALS: (
        frozenset({MentionType.LAB_VITAL}),
        frozenset({MentionType.PROBLEM}),
    ),
}


class Assertion(enum.Enum):
    """The master design's assertion set (§1 there); stage 4 owns the
    final value — sections only supply defaults."""

    PRESENT = "present"
    NEGATED = "negated"
    HISTORICAL = "historical"
    FAMILY_HISTORY = "family_history"
    UNCERTAIN = "uncertain"
    HYPOTHETICAL = "hypothetical"


class SectionKind(enum.Enum):
    """The family-medicine pack's segments (§5)."""

    HEADER = "header"
    SUBJECTIVE = "subjective"
    SUBJECTIVE_HISTORY = "subjective_history"
    SUBJECTIVE_FAMILY = "subjective_family"
    SUBJECTIVE_ALLERGY = "subjective_allergy"
    OBJECTIVE = "objective"
    ASSESSMENT = "assessment"
    PLAN = "plan"
    UNKNOWN = "unknown"


# §5: the section supplies the DEFAULT; stage 4 owns the final assertion.
SECTION_DEFAULT_ASSERTION: dict[SectionKind, Assertion | None] = {
    SectionKind.HEADER: None,  # no mentions extracted here
    SectionKind.SUBJECTIVE: Assertion.PRESENT,
    SectionKind.SUBJECTIVE_HISTORY: Assertion.HISTORICAL,
    SectionKind.SUBJECTIVE_FAMILY: Assertion.FAMILY_HISTORY,
    SectionKind.SUBJECTIVE_ALLERGY: Assertion.PRESENT,
    SectionKind.OBJECTIVE: Assertion.PRESENT,
    SectionKind.ASSESSMENT: Assertion.PRESENT,
    SectionKind.PLAN: Assertion.PRESENT,
    SectionKind.UNKNOWN: Assertion.PRESENT,
}


@dataclass(frozen=True)
class Section:
    """One deterministic segment of the note (§5)."""

    id: int
    kind: SectionKind
    span: Span


@dataclass(frozen=True)
class MentionAttribute:
    """One typed sub-span (§3); same verbatim invariant as mentions."""

    kind: AttributeKind
    span: Span
    text: str


@dataclass(frozen=True)
class Mention:
    """One span naming one clinical entity of one type (§1)."""

    id: int
    mention_type: MentionType
    span: Span
    text: str  # MUST equal note[start:end] — validator-enforced
    section_id: int
    attributes: tuple[MentionAttribute, ...] = ()


@dataclass(frozen=True)
class MentionRelation:
    """One composite clinical statement (§4)."""

    kind: RelationKind
    source_id: int
    target_id: int
    inferred: bool


@dataclass(frozen=True)
class SharedTrigger:
    """A section-local trigger recorded once and distributed by stage 4
    (§6): 'No fever, chills, or night sweats' — one span, three scopes."""

    section_id: int
    span: Span
    text: str


@dataclass(frozen=True)
class Extraction:
    """The validated output of stages 1–2 — the ONLY thing later stages see."""

    note_text: str
    sections: tuple[Section, ...]
    mentions: tuple[Mention, ...]
    relations: tuple[MentionRelation, ...] = ()
    shared_triggers: tuple[SharedTrigger, ...] = ()

    def section_of(self, mention: Mention) -> Section:
        """The mention's section (most specific segment)."""
        return next(s for s in self.sections if s.id == mention.section_id)
