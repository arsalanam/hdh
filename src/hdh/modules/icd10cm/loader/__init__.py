"""ICD-10-CM load pipeline: pluggable stages over a shared context.

The design's nine-stage pipeline (§4.2), realized as small ``LoadStage``
implementations composed by ``run_load`` — the same pluggable-check pattern
as the quality gate. Milestone B covers the order file end-to-end
(acquire → parse → structure → enrich → load → edges → verify → finalize);
the tabular-XML stages (blocks, Excludes1/2, code-first) join in
milestone C alongside ``--download``.

Every stage receives the same mutable :class:`LoadContext` and returns a
one-line summary; a raising stage aborts the load before ``finalize``
writes the ledger row, so a failed load is never recorded as complete.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class LoadError(Exception):
    """A load precondition, parse, or verification failure."""


@dataclass(frozen=True)
class CodeRow:
    """One parsed order-file row."""

    order: int
    code: str  # dotless, as in the file (e.g. "S52001A")
    dotted: str  # display form (e.g. "S52.001A")
    billable: bool
    short: str
    long: str


@dataclass
class LoadContext:
    """Shared state the stages read and extend, in order."""

    session: Session
    source_dir: Path
    fiscal_year: int
    force: bool = False
    started: float = field(default_factory=time.monotonic)
    # populated by stages:
    files: dict[str, Path] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    rows: list[CodeRow] = field(default_factory=list)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> column values
    edges: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)


class LoadStage(Protocol):
    """One pipeline stage: a name and a run() over the shared context."""

    name: ClassVar[str]

    def run(self, ctx: LoadContext) -> str:
        """Execute the stage; return a one-line summary for the report."""
        ...


def default_stages() -> tuple[LoadStage, ...]:
    """The milestone-B pipeline, in execution order."""
    from hdh.modules.icd10cm.loader import stages as s

    return (
        s.AcquireStage(),
        s.ParseStage(),
        s.StructureStage(),
        s.EnrichStage(),
        s.LoadConceptsStage(),
        s.EdgesStage(),
        s.VerifyStage(),
        s.FinalizeStage(),
    )


def run_load(
    session: Session,
    source_dir: str | Path,
    fiscal_year: int,
    force: bool = False,
    stages: tuple[LoadStage, ...] | None = None,
) -> list[tuple[str, str]]:
    """Run the pipeline; returns (stage name, summary) pairs in order."""
    ctx = LoadContext(session=session, source_dir=Path(source_dir), fiscal_year=fiscal_year, force=force)
    report = []
    for stage in stages if stages is not None else default_stages():
        report.append((stage.name, stage.run(ctx)))
    return report
