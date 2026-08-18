"""Stage-2 output validation (design §7): raw extractor output → typed
:class:`Extraction`, or a rejection whose reasons ARE the retry feedback.

Two span sources are accepted: explicit ``start``/``end`` (the stub
path) or ``text`` + ``occurrence`` resolved by the deterministic locator
(the LLM path — models are unreliable at character arithmetic; the
verbatim invariant must never depend on it). Every reason is collected,
not just the first: the extractor gets one complete correction list per
retry.
"""

from __future__ import annotations

from dataclasses import replace

from hdh.modules.comprehension.contracts import (
    ATTRIBUTE_LEGALITY,
    EMITTABLE_RELATIONS,
    RELATION_RULES,
    AttributeKind,
    Extraction,
    Mention,
    MentionAttribute,
    MentionRelation,
    MentionType,
    RelationKind,
    Section,
    SectionKind,
    SharedTrigger,
    Span,
)


class ExtractionError(Exception):
    """The extraction failed validation; ``reasons`` is the retry feedback."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _locate(note: str, item: dict, label: str, reasons: list[str]) -> Span | None:
    """Resolve an item's span: explicit offsets (verbatim-checked) or the
    nth occurrence of its text."""
    text = item.get("text", "")
    if not text:
        reasons.append(f"{label}: empty text")
        return None
    if "start" in item and "end" in item:
        span = Span(int(item["start"]), int(item["end"]))
        if note[span.start : span.end] != text:
            reasons.append(
                f"{label}: text {text!r} does not equal note[{span.start}:{span.end}] "
                f"({note[span.start : span.end]!r}) — spans must be verbatim"
            )
            return None
        return span
    occurrence = int(item.get("occurrence", 1))
    start = _nth_occurrence(note, text, occurrence)
    if start == -1:
        reasons.append(f"{label}: occurrence {occurrence} of {text!r} not found in the note")
        return None
    return Span(start, start + len(text))


def _nth_occurrence(note: str, text: str, occurrence: int) -> int:
    """The nth STANDALONE occurrence of text (not embedded mid-word — the
    extractor counts tokens, so "T" must never match the T in "NOTE");
    plain substring search is the fallback when no standalone match exists."""
    import re

    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(text)}(?![A-Za-z0-9])")
    matches = [m.start() for m in pattern.finditer(note)]
    if not matches:
        matches = []
        start = -1
        while True:
            start = note.find(text, start + 1)
            if start == -1:
                break
            matches.append(start)
    if len(matches) < occurrence:
        return -1
    return matches[occurrence - 1]


def _enum(value, enum_cls, label: str, reasons: list[str]):
    try:
        return enum_cls(str(value).lower())
    except ValueError:
        allowed = sorted(member.value for member in enum_cls)
        reasons.append(f"{label}: unknown value {value!r} (allowed: {allowed})")
        return None


def build_extraction(note: str, raw: dict, sections: tuple[Section, ...]) -> Extraction:
    """Validate raw extractor output against §7's rules; raise
    :class:`ExtractionError` with every collected reason on failure."""

    reasons: list[str] = []
    mentions: list[Mention] = []

    raw_to_built: dict[int, int] = {}
    for index, item in enumerate(raw.get("mentions", ())):
        built = _build_mention(note, sections, item, f"mention[{index}]", reasons)
        if built is None:
            continue
        raw_to_built[index] = len(mentions)
        mentions.append(replace(built, id=len(mentions)))

    mentions, raw_to_built = _collapse_contained(mentions, raw_to_built)
    _check_overlaps(mentions, reasons)
    relations = _build_relations(raw, mentions, raw_to_built, reasons)
    triggers = _build_triggers(note, raw, sections, reasons)

    if reasons:
        raise ExtractionError(reasons)
    return Extraction(
        note_text=note,
        sections=sections,
        mentions=tuple(mentions),
        relations=tuple(relations),
        shared_triggers=tuple(triggers),
    )


def _collapse_contained(
    mentions: list[Mention], raw_to_built: dict[int, int]
) -> tuple[list[Mention], dict[int, int]]:
    """A same-type mention whose span nests inside another's collapses
    into the larger one — models re-emit a diagnosis as its own
    indication ('hypertension' inside 'essential hypertension'); that is
    noise, not an error worth a retry. Attributes the survivor lacks are
    carried over; relations re-point via the returned raw map."""

    def container_of(index: int) -> int:
        mention = mentions[index]
        best = index
        for other_index, other in enumerate(mentions):
            if other.mention_type is not mention.mention_type:
                continue
            contains = other.span.start <= mention.span.start and mention.span.end <= other.span.end
            longer_or_first = len(other.text) > len(mentions[best].text) or (
                other.span == mentions[best].span and other_index < best
            )
            if other_index != index and contains and longer_or_first:
                best = other_index
        return best

    survivor = {index: container_of(index) for index in range(len(mentions))}
    kept: list[Mention] = []
    new_index: dict[int, int] = {}
    for index, mention in enumerate(mentions):
        if survivor[index] == index:
            new_index[index] = len(kept)
            kept.append(mention)
    for index, target in survivor.items():
        if target == index:
            continue
        keeper_id = new_index[target]
        keeper = kept[keeper_id]
        extra = tuple(
            attr
            for attr in mentions[index].attributes
            if all((attr.kind, attr.span) != (own.kind, own.span) for own in keeper.attributes)
        )
        if extra:
            kept[keeper_id] = replace(keeper, attributes=keeper.attributes + extra)
    renumbered = [replace(mention, id=position) for position, mention in enumerate(kept)]
    remapped = {raw: new_index[survivor[built]] for raw, built in raw_to_built.items()}
    return renumbered, remapped


def _attribute_spans(note: str, item: dict) -> list[Span]:
    """Where this mention's attributes actually sit — located without
    recording reasons, because this pass only informs occurrence choice."""
    spans = []
    for attr in item.get("attributes", ()):
        throwaway: list[str] = []
        span = _locate(note, attr, "probe", throwaway)
        if span is not None:
            spans.append(span)
    return spans


def _realign_to_attributes(note: str, sections, item: dict, span: Span) -> Span:
    """Pick the occurrence of the mention text that its own attributes
    point at.

    A word like "pain" appears both as a subjective complaint and as a
    vitals score; "BP" can appear in the plan and the objective panel.
    The extractor names the entity and its value correctly, but the
    occurrence index is the one thing models are unreliable about — so
    when the attributes land in a different section than the mention,
    prefer an occurrence whose section holds them. Every candidate is a
    verbatim occurrence, so the span invariant never bends."""
    from hdh.modules.comprehension.segment import section_for

    attribute_spans = _attribute_spans(note, item)
    if not attribute_spans:
        return span
    section = section_for(sections, span)
    if section is not None and all(
        section.span.start <= a.start and a.end <= section.span.end for a in attribute_spans
    ):
        return span  # already coherent

    text = item.get("text", "")
    anchor = min(a.start for a in attribute_spans)
    best: Span | None = None
    occurrence = 1
    while (start := _nth_occurrence(note, text, occurrence)) != -1 and occurrence <= 20:
        candidate = Span(start, start + len(text))
        candidate_section = section_for(sections, candidate)
        if candidate_section is not None and all(
            candidate_section.span.start <= a.start and a.end <= candidate_section.span.end
            for a in attribute_spans
        ):
            if best is None or abs(candidate.start - anchor) < abs(best.start - anchor):
                best = candidate
        occurrence += 1
    return best or span


def _build_mention(note: str, sections, item: dict, label: str, reasons: list[str]) -> Mention | None:
    """One validated mention, or None with the reasons recorded."""
    from hdh.modules.comprehension.segment import section_for

    mention_type = _enum(item.get("type"), MentionType, label, reasons)
    span = _locate(note, item, label, reasons)
    if mention_type is None or span is None:
        return None
    span = _realign_to_attributes(note, sections, item, span)
    section = section_for(sections, span)
    if section is None:
        reasons.append(f"{label}: span {span} lies outside every section")
        return None
    if section.kind is SectionKind.HEADER:
        reasons.append(f"{label}: mentions are not extracted from the note header")
        return None

    attributes: list[MentionAttribute] = []
    for a_index, attr in enumerate(item.get("attributes", ())):
        a_label = f"{label}.attribute[{a_index}]"
        kind = _enum(attr.get("kind"), AttributeKind, a_label, reasons)
        a_span = _locate(note, attr, a_label, reasons)
        if kind is None or a_span is None:
            continue
        if mention_type not in ATTRIBUTE_LEGALITY[kind]:
            reasons.append(f"{a_label}: kind '{kind.value}' is illegal on a {mention_type.value} mention")
            continue
        if not (section.span.start <= a_span.start and a_span.end <= section.span.end):
            reasons.append(
                f"{a_label}: attribute {attr['text']!r} sits in a different section than the mention "
                f"{item['text']!r} — point them at the same occurrence"
            )
            continue
        attributes.append(MentionAttribute(kind=kind, span=a_span, text=attr["text"]))

    return Mention(
        id=0,  # renumbered by the caller
        mention_type=mention_type,
        span=span,
        text=item["text"],
        section_id=section.id,
        attributes=tuple(attributes),
    )


def _check_overlaps(mentions: list[Mention], reasons: list[str]) -> None:
    """§2 rule 5: same-type head spans may not overlap (nesting across
    types is legal)."""
    by_type: dict[MentionType, list[Mention]] = {}
    for mention in mentions:
        by_type.setdefault(mention.mention_type, []).append(mention)
    for mention_type, group in by_type.items():
        ordered = sorted(group, key=lambda m: m.span)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.span.start < left.span.end:
                reasons.append(
                    f"two {mention_type.value} mentions overlap: "
                    f"{left.text!r} {left.span} and {right.text!r} {right.span}"
                )


def _build_relations(
    raw: dict, mentions: list[Mention], raw_to_built: dict[int, int], reasons: list[str]
) -> list[MentionRelation]:
    """Relation indexes refer to the extractor's RAW mentions array — map
    them onto the surviving built mentions, never a shifted index.

    A relation whose endpoint is missing is dropped silently: relations
    are optional hints, so a bad index must not cost the whole note. Type
    and kind violations stay errors — both endpoints exist, so the model
    made a real semantic mistake it can correct."""
    relations: list[MentionRelation] = []
    for index, item in enumerate(raw.get("relations", ())):
        label = f"relation[{index}]"
        kind = _enum(item.get("kind"), RelationKind, label, reasons)
        if kind is None:
            continue
        if kind not in EMITTABLE_RELATIONS:
            reasons.append(f"{label}: '{kind.value}' is reserved until its consumer lands — emit TREATS only")
            continue
        raw_source, raw_target = item.get("source"), item.get("target")
        if not isinstance(raw_source, int) or not isinstance(raw_target, int):
            reasons.append(f"{label}: source/target must be integer mention indexes")
            continue
        if raw_source not in raw_to_built or raw_target not in raw_to_built:
            # Dangling endpoint: either an out-of-range index, or a mention
            # that failed its own validation (and is already reported).
            # A relation is a HINT (§4: inferred relations "are hints for
            # downstream stages, never facts"), so losing one must not
            # discard the note's every mention, vital and medication. Live
            # eval: a note failed all 3 retries on this alone, because the
            # feedback said "fix that mention first" — unactionable when
            # the mention was never emitted. Dropped like a duplicate.
            continue
        source_id, target_id = raw_to_built[raw_source], raw_to_built[raw_target]
        source_types, target_types = RELATION_RULES[kind]
        source, target = mentions[source_id], mentions[target_id]
        if source.mention_type not in source_types or target.mention_type not in target_types:
            reasons.append(
                f"{label}: {kind.value} requires source in "
                f"{sorted(t.value for t in source_types)} and target in "
                f"{sorted(t.value for t in target_types)}, got "
                f"{source.mention_type.value} → {target.mention_type.value}"
            )
            continue
        relation = MentionRelation(
            kind=kind, source_id=source_id, target_id=target_id, inferred=bool(item.get("inferred", True))
        )
        if relation not in relations:  # exact duplicates are noise, not errors — collapse silently
            relations.append(relation)
    return relations


def _build_triggers(
    note: str, raw: dict, sections: tuple[Section, ...], reasons: list[str]
) -> list[SharedTrigger]:
    from hdh.modules.comprehension.segment import section_for

    triggers: list[SharedTrigger] = []
    for index, item in enumerate(raw.get("shared_triggers", ())):
        label = f"shared_trigger[{index}]"
        span = _locate(note, item, label, reasons)
        if span is None:
            continue
        section = section_for(sections, span)
        if section is None:
            reasons.append(f"{label}: span lies outside every section")
            continue
        triggers.append(SharedTrigger(section_id=section.id, span=span, text=item["text"]))
    return triggers
