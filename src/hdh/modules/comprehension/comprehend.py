"""Stages 1–2 orchestration: segment, extract, validate, retry, or fail loudly.

The loader discipline applied to comprehension (master §3): a note that
cannot produce a valid extraction after ``max_attempts`` raises — nothing
half-extracted continues down the pipeline or reaches storage.
"""

from __future__ import annotations

from hdh.modules.comprehension.contracts import Extraction
from hdh.modules.comprehension.extract import Extractor
from hdh.modules.comprehension.segment import segment
from hdh.modules.comprehension.validate import ExtractionError, build_extraction

MAX_ATTEMPTS = 3


class ComprehensionError(Exception):
    """The note could not be validly extracted within the attempt budget."""


def comprehend_text(note_text: str, extractor: Extractor, max_attempts: int = MAX_ATTEMPTS) -> Extraction:
    """Run stages 1–2 with the retry-with-feedback loop; return the
    validated Extraction or raise loudly."""
    sections = segment(note_text)
    feedback: str | None = None
    last_reasons: list[str] = []
    for _attempt in range(max_attempts):
        raw = extractor(note_text, sections, feedback)
        try:
            return build_extraction(note_text, raw, sections)
        except ExtractionError as err:
            last_reasons = err.reasons
            feedback = "\n".join(f"- {reason}" for reason in err.reasons)
    raise ComprehensionError(
        f"extraction failed validation after {max_attempts} attempts; last reasons: "
        + "; ".join(last_reasons[:5])
    )


def render_report(extraction: Extraction) -> str:
    """A human-readable view of the validated extraction (the CLI output).

    Assertions shown are the SECTION DEFAULTS — provisional until stage 4
    (contextualize) lands in milestone B; codes arrive with stage 3."""
    from hdh.modules.comprehension.contracts import SECTION_DEFAULT_ASSERTION

    lines = [f"sections: {len(extraction.sections)} · mentions: {len(extraction.mentions)}"]
    for mention in extraction.mentions:
        section = extraction.section_of(mention)
        default = SECTION_DEFAULT_ASSERTION.get(section.kind)
        attrs = ", ".join(f"{a.kind.value}={a.text!r}" for a in mention.attributes)
        lines.append(
            f"  [{mention.id}] {mention.mention_type.value:<10} {mention.text!r} "
            f"@{mention.span.start}..{mention.span.end} "
            f"({section.kind.value}; default assertion: {default.value if default else '—'})"
            + (f"  {{{attrs}}}" if attrs else "")
        )
    for relation in extraction.relations:
        source = extraction.mentions[relation.source_id]
        target = extraction.mentions[relation.target_id]
        flag = " (inferred)" if relation.inferred else ""
        lines.append(f"  relation: {source.text!r} —{relation.kind.value}→ {target.text!r}{flag}")
    for trigger in extraction.shared_triggers:
        lines.append(f"  shared trigger: {trigger.text!r} @{trigger.span.start}")
    return "\n".join(lines)
