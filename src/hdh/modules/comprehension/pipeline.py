"""Stages 3–5 orchestration + storage: the comprehended note.

``comprehend_note`` takes a validated Extraction (stages 1–2) and
produces coded, asserted, confidence-carrying mentions; ``store_record``
writes them to the registry entities inside one transaction. The
disambiguation stage (5) is the H54 lesson in miniature: when the
funnel's top candidates are close, the candidate whose SNOMED ancestors
overlap the note's OTHER coded problems wins — identical words,
different subtrees, different patients.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hdh.modules.comprehension.contextualize import AssertionResult, finalize_assertion
from hdh.modules.comprehension.contracts import Extraction, Mention
from hdh.modules.comprehension.normalize import Code, MentionNormalizer

PIPELINE_VERSION = "0.2"  # milestone B: stages 1–5
REVIEW_THRESHOLD = 0.6  # any mention below → record needs_review
_CLOSE_CALL = 0.15  # score gap that makes candidates "ambiguous" (stage 5)
_CONTEXT_BOOST = 0.2


@dataclass(frozen=True)
class ComprehendedMention:
    """One mention after stages 3–5: coded (or honestly unlinked),
    asserted with evidence, confidence-carrying."""

    mention: Mention
    code: Code | None
    assertion: AssertionResult
    confidence: float


@dataclass(frozen=True)
class ComprehendedNote:
    """The full stages-1–5 result for one note."""

    extraction: Extraction
    mentions: tuple[ComprehendedMention, ...]

    @property
    def needs_review(self) -> bool:
        return any(m.confidence < REVIEW_THRESHOLD for m in self.mentions)


def comprehend_note(session, extraction: Extraction) -> ComprehendedNote:
    """Run normalize (3), contextualize (4), disambiguate (5)."""
    normalizer = MentionNormalizer(session)
    candidate_sets = {m.id: normalizer.candidates(m) for m in extraction.mentions}
    context = _context_ancestors(session, extraction, candidate_sets)

    comprehended = []
    for mention in extraction.mentions:
        code = _disambiguate(session, mention, candidate_sets[mention.id], context)
        assertion = finalize_assertion(extraction, mention)
        confidence = round(code.score if code else 0.3, 3)
        comprehended.append(
            ComprehendedMention(mention=mention, code=code, assertion=assertion, confidence=confidence)
        )
    return ComprehendedNote(extraction=extraction, mentions=tuple(comprehended))


def _context_ancestors(session, extraction: Extraction, candidate_sets) -> frozenset[str]:
    """SNOMED ancestor codes of every UNAMBIGUOUS problem in the note —
    the context that settles ambiguous ones (stage 5)."""
    from hdh.core.ontology import get_ontology_service

    service = get_ontology_service("snomed_ct", session)
    ancestors: set[str] = set()
    for mention in extraction.mentions:
        candidates = candidate_sets[mention.id]
        if not candidates or candidates[0].system != "snomed_ct":
            continue
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < _CLOSE_CALL:
            continue  # ambiguous — contributes nothing until settled
        for concept in service.ancestors(candidates[0].code):
            ancestors.add(concept.code)
    return frozenset(ancestors)


def _disambiguate(session, mention: Mention, candidates: tuple[Code, ...], context: frozenset[str]):
    """Top candidate wins unless a close runner-up sits inside the note's
    ancestor context (then the context wins)."""
    if not candidates:
        return None
    best = candidates[0]
    if len(candidates) == 1 or best.system != "snomed_ct" or not context:
        return best
    from hdh.core.ontology import get_ontology_service

    service = get_ontology_service("snomed_ct", session)
    rescored = []
    for candidate in candidates:
        if best.score - candidate.score > _CLOSE_CALL:
            rescored.append((candidate.score, candidate))
            continue
        candidate_ancestors = {c.code for c in service.ancestors(candidate.code)}
        boost = _CONTEXT_BOOST if candidate_ancestors & context else 0.0
        rescored.append((candidate.score + boost, candidate))
    rescored.sort(key=lambda pair: -pair[0])
    score, winner = rescored[0]
    return Code(
        winner.system, winner.code, winner.display, round(min(score, 1.0), 3), winner.in_shared_tables
    )


def store_record(session, visit_note_id: int, note: ComprehendedNote, pack: str = "family_medicine") -> int:
    """Write NoteRecord + NoteMention rows in one transaction; returns the
    record id. Attributes/relations travel in properties JSON (design §8);
    ``concept_id`` is set ONLY for codes that live in the shared tables."""
    from sqlalchemy import insert

    from hdh.core.models import Base

    tables = Base.metadata.tables
    records_t, mentions_t = tables["note_records"], tables["note_mentions"]
    status = "needs_review" if note.needs_review else "complete"
    record_id = session.execute(
        insert(records_t).values(
            visit_note_id=visit_note_id,
            pack=pack,
            pipeline_version=PIPELINE_VERSION,
            status=status,
            created_at=datetime.now(),
            properties={
                "relations": [
                    {
                        "kind": r.kind.value,
                        "source": r.source_id,
                        "target": r.target_id,
                        "inferred": r.inferred,
                    }
                    for r in note.extraction.relations
                ]
            },
        )
    ).inserted_primary_key[0]
    rows = []
    for item in note.mentions:
        mention = item.mention
        rows.append(
            {
                "record_id": record_id,
                "mention_type": mention.mention_type.value,
                "start": mention.span.start,
                "end": mention.span.end,
                "text": mention.text,
                "section_kind": note.extraction.section_of(mention).kind.value,
                "assertion": item.assertion.assertion.value,
                "concept_id": (
                    f"snomed_ct:{item.code.code}" if item.code and item.code.in_shared_tables else None
                ),
                "confidence": item.confidence,
                "properties": {
                    "attributes": [
                        {"kind": a.kind.value, "start": a.span.start, "end": a.span.end, "text": a.text}
                        for a in mention.attributes
                    ],
                    "assertion_evidence": item.assertion.evidence,
                    "code": (
                        {"system": item.code.system, "code": item.code.code, "display": item.code.display}
                        if item.code
                        else None
                    ),
                },
            }
        )
    if rows:
        session.execute(insert(mentions_t), rows)
    session.commit()
    return record_id
