"""Description→code retrieval: the design §7.2 funnel.

The LLM only ever classifies (stage 1, behind the injectable
``AxisExtractor`` callable); every stage after it is deterministic SQL +
scoring, so the funnel is reproducible and testable offline with a stub
extractor. Results are frozen dataclasses carrying their explanation —
which axes matched, conflicted, or were never stated — so a consumer (the
chat agent, the care-plan module) can show *why* and ask about what's
missing instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import or_, select

# The recognized clinical axes and their closed value sets. Values double
# as display-text evidence: a candidate that states a *different* value of
# a requested axis is a conflict, one that states none is a gap.
AXIS_VALUES: dict[str, tuple[str, ...]] = {
    "laterality": ("right", "left", "bilateral", "unspecified"),
    "aspect": ("medial", "lateral", "anterior", "posterior", "superior", "inferior"),
    "displacement": ("displaced", "nondisplaced"),
    "encounter": ("initial", "subsequent", "sequela"),
    "exposure": ("open", "closed"),
}

ANCHOR_POOL = 12  # top search hits whose subtrees seed the candidate pool
CANDIDATE_POOL = 400  # max billable candidates scored per call


class CodifyError(Exception):
    """A funnel precondition failure (empty catalog, bad extraction…)."""


@dataclass(frozen=True)
class AxisExtraction:
    """Stage-1 output: canonical search terms + the stated clinical axes."""

    terms: str
    axes: dict[str, str]

    def validated(self) -> AxisExtraction:
        """Drop unknown axes/values rather than let them poison scoring."""
        clean = {
            axis: value.lower()
            for axis, value in self.axes.items()
            if axis in AXIS_VALUES and value.lower() in AXIS_VALUES[axis]
        }
        return AxisExtraction(self.terms.strip(), clean)


AxisExtractor = Callable[[str], AxisExtraction]


@dataclass(frozen=True)
class CodedCandidate:
    """One ranked suggestion with its full explanation."""

    code: str
    display: str
    is_billable: bool
    path: str
    matched: tuple[str, ...]  # axes the candidate satisfies
    conflicts: tuple[str, ...]  # axes where it states a different value
    unstated: tuple[str, ...]  # requested axes the description never pinned down

    @property
    def exact(self) -> bool:
        return not self.conflicts and not self.unstated


def _axis_evidence(display: str, axes_props: dict, axis: str, wanted: str) -> str:
    """Classify one candidate against one requested axis:
    'match' | 'conflict' | 'unstated'."""
    stated = (axes_props or {}).get(axis)
    if stated is not None:
        return "match" if stated == wanted else "conflict"
    lowered = display.lower()
    if re.search(rf"\b{wanted}\b", lowered):
        return "match"
    for other in AXIS_VALUES[axis]:
        if other != wanted and re.search(rf"\b{other}\b", lowered):
            return "conflict"
    return "unstated"


def _score(candidate_row, axes: dict[str, str]) -> CodedCandidate:
    matched: list[str] = []
    conflicts: list[str] = []
    unstated: list[str] = []
    axes_props = (candidate_row.properties or {}).get("axes", {})
    for axis, wanted in sorted(axes.items()):
        verdict = _axis_evidence(candidate_row.display, axes_props, axis, wanted)
        {"match": matched, "conflict": conflicts, "unstated": unstated}[verdict].append(axis)
    return CodedCandidate(
        code=candidate_row.code,
        display=candidate_row.display,
        is_billable=candidate_row.is_billable,
        path=candidate_row.path,
        matched=tuple(matched),
        conflicts=tuple(conflicts),
        unstated=tuple(unstated),
    )


def codify(
    session,
    description: str,
    extractor: AxisExtractor,
    limit: int = 5,
) -> tuple[AxisExtraction, list[CodedCandidate]]:
    """Run the funnel: extract → anchor → descend → score → rank.

    Returns the (validated) extraction alongside the ranked candidates so
    callers can surface what was understood and what was never stated.
    """
    from hdh.core.models import Base
    from hdh.modules.icd10cm.cli import search_concepts

    extraction = extractor(description).validated()
    if not extraction.terms:
        raise CodifyError("axis extraction produced no search terms")

    concepts_t = Base.metadata.tables["ontology_concepts"]
    anchors = search_concepts(session, extraction.terms, ANCHOR_POOL)
    if not anchors:
        return extraction, []
    anchor_paths = [
        session.execute(
            select(concepts_t.c.path).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code == row.code)
        ).scalar()
        for row in anchors
    ]
    # Widen leaf hits to their subcategory subtree (chapter.block.category.sub)
    # — the funnel anchors on candidate FAMILIES, so sibling branches the
    # search didn't surface (the nondisplaced variant, the other episodes)
    # still enter the pool and can be scored (design §7.2 stage 2→3).
    subtree_roots = sorted({".".join(p.split(".")[:4]) for p in anchor_paths if p})
    prefix_filters = [or_(concepts_t.c.path == p, concepts_t.c.path.like(p + ".%")) for p in subtree_roots]
    pool = session.execute(
        select(
            concepts_t.c.code,
            concepts_t.c.display,
            concepts_t.c.is_billable,
            concepts_t.c.path,
            concepts_t.c.properties,
        )
        .where(concepts_t.c.is_billable, or_(*prefix_filters))
        .limit(CANDIDATE_POOL)
    ).all()

    scored = [_score(row, extraction.axes) for row in pool]
    anchor_order = {row.code: i for i, row in enumerate(anchors)}
    scored.sort(
        key=lambda c: (
            -len(c.matched),
            len(c.conflicts),
            anchor_order.get(c.code, ANCHOR_POOL),
            -c.path.count("."),  # specificity: deeper wins
            c.code,
        )
    )
    return extraction, scored[:limit]


def stub_extractor(terms: str, axes: dict[str, str]) -> AxisExtractor:
    """A fixed extraction — tests and the CLI's offline --terms/--axes path."""

    def extract(_description: str) -> AxisExtraction:
        return AxisExtraction(terms, dict(axes))

    return extract


def ambiguous_axes(candidates: list[CodedCandidate], requested: dict[str, str]) -> tuple[str, ...]:
    """Axes the top candidates DISAGREE on that the caller never stated —
    the funnel's cue to ask a follow-up question ("was the fracture
    displaced?") instead of silently picking a branch (design §7.2, RFC Q9).
    """
    seen: dict[str, set[str]] = {}
    for candidate in candidates:
        lowered = candidate.display.lower()
        for axis, options in AXIS_VALUES.items():
            if axis in requested:
                continue
            for value in options:
                if re.search(rf"\b{value}\b", lowered):
                    seen.setdefault(axis, set()).add(value)
                    break
    return tuple(sorted(axis for axis, values in seen.items() if len(values) > 1))
