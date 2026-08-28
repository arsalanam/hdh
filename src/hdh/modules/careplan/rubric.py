"""The rubric a plan is graded against: JSON on disk, validated on load.

Design §9. A rubric is a set of **dimensions**, each with a question, an
anchored 1-n scale, and the list of deterministic facts the grader should
be *told* rather than asked to work out. Rubrics are selected per
archetype — an older patient on many medications is graded against
different anchors than a single-condition adult.

Two things about the format are deliberate.

**Rubrics are files, not corpus rows.** The design first put them in the
knowledge store alongside the clinical documents. But a rubric is
structured — dimensions, scales, thresholds — and retrieval returns prose.
Keeping them as JSON means they are validated on load, diffable in review,
and usable with no database at all.

**Every dimension cites a source.** The same rule the corpus enforces
(``CorpusError`` on a document with no source) applies here, for the same
reason: a rubric asserts what good care looks like, and one that cannot
say where that standard came from is an opinion wearing a score.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Rubrics that ship with the module.
BUNDLED = pathlib.Path(__file__).resolve().parent / "rubrics"

#: The archetype constraints a rubric may match on. Every one is derived
#: from :class:`~hdh.modules.careplan.context.CarePlanContext` by
#: arithmetic — which rubric applies is not a judgement, and asking a model
#: to classify something countable would make it one.
MATCH_KEYS = ("min_age", "max_age", "min_problems", "min_medications")

#: The generating nodes a failing dimension can be routed back to (§9), in
#: pipeline order. Revising a node regenerates it and everything after it,
#: so the earliest failing node governs — regenerating concerns and keeping
#: the goals that were written for the old ones would leave a graph whose
#: edges no longer mean anything.
REVISABLE_NODES = ("concerns", "goals", "interventions")


class RubricError(RuntimeError):
    """A rubric on disk is not usable as one."""


@dataclass(frozen=True)
class Dimension:
    """One thing a plan is scored on."""

    id: str
    title: str
    question: str
    source: str
    facts: tuple[str, ...]
    anchors: Mapping[int, str]
    #: Which generating node a low score on this dimension routes back to.
    #: Declared per dimension because the rubric knows what it is judging:
    #: vague goals are node 4's problem, an unanswered flag is node 5's.
    revises: str

    @property
    def node_order(self) -> int:
        return REVISABLE_NODES.index(self.revises)

    def anchor_lines(self) -> list[str]:
        """The scale, described, lowest first — what the grader is shown."""
        return [f"{level} — {self.anchors[level]}" for level in sorted(self.anchors)]


@dataclass(frozen=True)
class Rubric:
    """A scored template for one plan archetype."""

    rubric_id: str
    version: int
    title: str
    match: Mapping[str, int]
    scale_min: int
    scale_max: int
    revise_below: int
    fail_below: int
    dimensions: tuple[Dimension, ...]

    @property
    def specificity(self) -> int:
        """How many constraints this rubric asserts. Most specific wins."""
        return len(self.match)

    def matches(self, context) -> bool:
        """Does this rubric's archetype describe this patient?

        Every declared constraint must hold. A rubric with no constraints
        matches everyone, which is how ``default`` becomes the fallback
        without being special-cased anywhere.
        """
        available = {
            "min_age": context.age >= self.match.get("min_age", 0),
            "max_age": context.age <= self.match.get("max_age", 0),
            "min_problems": len(context.problems) >= self.match.get("min_problems", 0),
            "min_medications": len(context.medications) >= self.match.get("min_medications", 0),
        }
        return all(available[key] for key in self.match)

    def dimension(self, dimension_id: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.id == dimension_id), None)


def _require(block: Mapping, key: str, where: str):
    if key not in block:
        raise RubricError(f"{where} is missing {key!r}")
    return block[key]


def _parse_dimension(raw: Mapping, where: str, scale: range, known_facts: frozenset[str]) -> Dimension:
    for key in ("id", "title", "question", "source", "anchors", "revises"):
        _require(raw, key, where)
    name = raw["id"]
    if not str(raw["source"]).strip():
        raise RubricError(
            f"{where}: dimension {name!r} has an empty source — a rubric that cannot say "
            "where its standard came from is an opinion wearing a score"
        )

    anchors: dict[int, str] = {}
    for level, text in dict(raw["anchors"]).items():
        try:
            value = int(level)
        except (TypeError, ValueError):
            raise RubricError(f"{where}: dimension {name!r} anchor key {level!r} is not a number") from None
        if value not in scale:
            raise RubricError(
                f"{where}: dimension {name!r} anchors level {value}, outside the scale "
                f"{scale.start}-{scale.stop - 1}"
            )
        if not str(text).strip():
            raise RubricError(f"{where}: dimension {name!r} anchor {value} is empty")
        anchors[value] = str(text)

    # Both ends must be described. A scale whose top or bottom is undefined
    # asks the grader to invent what a 1 or a 5 means, and the whole reason
    # for anchoring is that it should not have to.
    for end in (scale.start, scale.stop - 1):
        if end not in anchors:
            raise RubricError(f"{where}: dimension {name!r} does not anchor level {end}")

    facts = tuple(str(fact) for fact in raw.get("facts", ()))
    unknown = [fact for fact in facts if fact not in known_facts]
    if unknown:
        raise RubricError(
            f"{where}: dimension {name!r} asks for unknown fact(s) {', '.join(unknown)} — "
            f"known facts are {', '.join(sorted(known_facts))}"
        )
    revises = str(raw["revises"])
    if revises not in REVISABLE_NODES:
        raise RubricError(
            f"{where}: dimension {name!r} revises {revises!r}, which is not a generating node — "
            f"expected one of {', '.join(REVISABLE_NODES)}"
        )
    return Dimension(
        id=str(name),
        title=str(raw["title"]),
        question=str(raw["question"]),
        source=str(raw["source"]),
        facts=facts,
        anchors=anchors,
        revises=revises,
    )


def parse_rubric(raw: Mapping, where: str = "rubric") -> Rubric:
    """Validate one rubric document. Raises :class:`RubricError`."""
    known_facts = _known_facts()
    for key in ("rubric_id", "version", "title", "scale", "thresholds", "dimensions"):
        _require(raw, key, where)

    scale_block = raw["scale"]
    low = int(_require(scale_block, "min", where))
    high = int(_require(scale_block, "max", where))
    if low >= high:
        raise RubricError(f"{where}: scale min {low} is not below max {high}")
    scale = range(low, high + 1)

    thresholds = raw["thresholds"]
    revise_below = int(_require(thresholds, "revise_below", where))
    fail_below = int(_require(thresholds, "fail_below", where))
    if not low < fail_below <= revise_below <= high:
        raise RubricError(
            f"{where}: thresholds must satisfy {low} < fail_below <= revise_below <= {high}, "
            f"got fail_below={fail_below}, revise_below={revise_below}"
        )

    match = dict(raw.get("match") or {})
    unknown = [key for key in match if key not in MATCH_KEYS]
    if unknown:
        raise RubricError(f"{where}: unknown match key(s) {', '.join(unknown)}")

    dimensions = tuple(
        _parse_dimension(block, where, scale, known_facts) for block in _require(raw, "dimensions", where)
    )
    if not dimensions:
        raise RubricError(f"{where} has no dimensions")
    ids = [dimension.id for dimension in dimensions]
    if len(set(ids)) != len(ids):
        raise RubricError(f"{where}: duplicate dimension id(s)")

    return Rubric(
        rubric_id=str(raw["rubric_id"]),
        version=int(raw["version"]),
        title=str(raw["title"]),
        match=match,
        scale_min=low,
        scale_max=high,
        revise_below=revise_below,
        fail_below=fail_below,
        dimensions=dimensions,
    )


def _known_facts() -> frozenset[str]:
    from hdh.modules.careplan.facts import FACTS

    return frozenset(FACTS)


def load_rubrics(root: pathlib.Path | None = None) -> list[Rubric]:
    """Every rubric on disk, validated.

    The filename carries the id — the same convention ``corpus.json``
    already uses for corpus directories, so a rubric cannot be renamed in
    one place and referenced by its old name in another.
    """
    base = root or BUNDLED
    if not base.is_dir():
        raise RubricError(f"no rubric directory: {base}")
    rubrics: list[Rubric] = []
    for path in sorted(base.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise RubricError(f"{path.name}: not valid JSON: {err}") from None
        rubric = parse_rubric(raw, where=path.name)
        if rubric.rubric_id != path.stem:
            raise RubricError(f"{path.name}: rubric_id {rubric.rubric_id!r} does not match the filename")
        rubrics.append(rubric)
    if not rubrics:
        raise RubricError(f"{base} contains no rubrics")
    return rubrics


def select_rubric(context, rubrics: Sequence[Rubric] | None = None) -> Rubric:
    """The most specific rubric whose archetype fits this patient.

    Ties break on ``rubric_id`` so the choice is reproducible: a plan
    graded today and re-graded next month must be graded against the same
    rubric, and "whichever one the filesystem listed first" is not that.
    """
    pool = rubrics if rubrics is not None else load_rubrics()
    candidates = [rubric for rubric in pool if rubric.matches(context)]
    if not candidates:
        raise RubricError(
            f"no rubric matches this patient (age {context.age}, {len(context.problems)} problem(s), "
            f"{len(context.medications)} medication(s)) — a rubric with an empty match block is "
            "required as the fallback"
        )
    return sorted(candidates, key=lambda rubric: (-rubric.specificity, rubric.rubric_id))[0]
