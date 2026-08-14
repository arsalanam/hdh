"""SNOMED CT load pipeline: pluggable stages over a shared context.

The design's eight-stage pipeline (snomed-module.md §4), in the house
``LoadStage`` shape (the icd10cm loader's pattern, re-stated here — modules
never import each other's internals). Every stage receives the same
mutable :class:`LoadContext` and returns a one-line summary; a raising
stage aborts before ``finalize`` writes the ledger row, so a failed load
is never recorded as complete.

Milestone B runs the pipeline from a ``--source`` RF2 directory (the
committed synthetic fixture or any legitimately obtained extract); the
UTS download joins in milestone C.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

ONTOLOGY = "snomed_ct"


class LoadError(Exception):
    """A load precondition, parse, or verification failure."""


@dataclass
class LoadContext:
    """Shared state the stages read and extend, in order."""

    session: Session
    source_dir: Path
    release: int | None = None  # YYYYMM; detected from filenames if None
    force: bool = False
    started: float = field(default_factory=time.monotonic)
    # populated by stages:
    files: dict[str, Path] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> concept column values
    terms: list[dict[str, Any]] = field(default_factory=list)  # ontology_terms rows
    edges: list[dict[str, Any]] = field(default_factory=list)  # ontology_edges rows
    parents: dict[str, list[str]] = field(default_factory=dict)  # child id -> parent ids (is-a)
    counters: dict[str, int] = field(default_factory=dict)


class LoadStage(Protocol):
    """One pipeline stage: a name and a run() over the shared context."""

    name: ClassVar[str]

    def run(self, ctx: LoadContext) -> str:
        """Execute the stage; return a one-line summary for the report."""
        ...


def default_stages() -> tuple[LoadStage, ...]:
    """The milestone-B pipeline, in execution order."""
    from hdh.modules.snomed.loader import stages as s

    return (
        s.AcquireStage(),
        s.ParseStage(),
        s.BuildStage(),
        s.LoadRowsStage(),
        s.ClosureStage(),
        s.AccelerateStage(),
        s.VerifyStage(),
        s.FinalizeStage(),
    )


def run_load(
    session: Session,
    source_dir: str | Path,
    release: int | None = None,
    force: bool = False,
    stages: tuple[LoadStage, ...] | None = None,
) -> list[tuple[str, str]]:
    """Run the pipeline; returns (stage name, summary) pairs in order."""
    ctx = LoadContext(session=session, source_dir=Path(source_dir), release=release, force=force)
    report = []
    for stage in stages if stages is not None else default_stages():
        report.append((stage.name, stage.run(ctx)))
    return report
