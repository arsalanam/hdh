"""Licensed cross-vocabulary map loaders (issue #86, part 2).

The `maps_to` edge model is ontology-agnostic and already carries three
*derived* authorities (`ontology/derive.py`): the pack author's codes, the
curated starter map, and the normalize() funnel. What it did not have is a
path to load an **official** crosswalk — the NLM ICD-10-CM ↔ SNOMED CT map,
and LOINC's equivalents — the maps a licensee downloads but nobody may
redistribute.

Same rule as SNOMED CT, LOINC and RxNorm: **the loader ships, the data never
does.** A user points `--source` at a file they obtained under their own
licence; `scripts/release_check.py` continues to refuse any asset that
contains the rows such a load produces (the edges target `snomed_ct:` and are
already caught), and `hdh snomed purge` removes them. Nothing licensed is
committed — only a synthetic fixture in each file's format, enough to test
the parser end to end and no further.

The pipeline is the house ``LoadStage`` shape (the SNOMED and ICD-10-CM
loaders' pattern, re-stated — modules never import each other's internals):
every stage receives the same mutable :class:`LoadContext` and returns a
one-line summary, and a raising stage aborts before :class:`FinalizeStage`
writes the ledger row, so a failed load is never recorded as complete.

One map differs from the next only in its *format* — which delimited columns
carry the two codes — so a single :class:`MapSpec` parameterises the whole
pipeline and the two named maps are two specs. A third map is a third spec,
not a third loader.

**It cannot be verified end to end here.** The NLM and LOINC maps are
licensed and do not ship, so the parser is exercised against a synthetic
fixture in the same format and no further. Whoever first runs this against a
real UMLS or LOINC release should expect it to find something the fixture
did not — this repository has a habit of its real defects surfacing only on
real data.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class LoadError(Exception):
    """A load precondition, parse, or verification failure."""


def _dot_icd10(code: str) -> str:
    """Normalise an ICD-10-CM code to the dotted form the catalog stores.

    NLM's map carries codes dotless (``E119``); the loaded concept id is
    ``icd10cm:E11.9``. Dot after the third character, matching the ICD-10-CM
    loader's own rule — an already-dotted code passes through unchanged.
    """
    code = code.strip()
    if "." in code or len(code) <= 3:
        return code
    return f"{code[:3]}.{code[3:]}"


def _verbatim(code: str) -> str:
    """A code whose file form already matches its concept id (LOINC, SNOMED)."""
    return code.strip()


@dataclass(frozen=True)
class MapSpec:
    """How to read one official map file and where its edges point.

    A map is a delimited file with a header; ``source_col`` and ``target_col``
    name the two columns that carry the codes, ``source_ontology`` /
    ``target_ontology`` the vocabularies they belong to, and ``authority`` the
    provenance stamped on every edge (and the only authority this load
    rebuilds — foreign edges, including the derived ones, are never touched).
    """

    name: str  # the --map key
    authority: str  # edge authority, e.g. "NLM_UMLS"
    ledger_tag: str  # ontology_loads.ontology, ≤16 chars, e.g. "icd10cm_snomed"
    source_ontology: str
    target_ontology: str
    source_col: str
    target_col: str
    description: str
    delimiter: str = "\t"
    display_col: str | None = None  # a human label for the target, if present
    onetoone_col: str | None = None  # truthy ⇒ confidence 1.0, else many_confidence
    category_col: str | None = None  # kept in edge properties when present
    many_confidence: float = 0.8  # confidence for a one-to-many row
    default_confidence: float = 1.0  # confidence when there is no one-to-one column
    normalise_source: Callable[[str], str] = _verbatim

    def source_id(self, code: str) -> str:
        """The shared-table concept id for a source code in this map."""
        return f"{self.source_ontology}:{self.normalise_source(code)}"

    def target_id(self, code: str) -> str:
        """The shared-table concept id for a target code in this map."""
        return f"{self.target_ontology}:{code.strip()}"


#: The maps named in issue #86, each a format the same pipeline loads. A new
#: licensed map is a new entry here, not a new module.
SPECS: dict[str, MapSpec] = {
    "nlm-icd10cm-snomed": MapSpec(
        name="nlm-icd10cm-snomed",
        authority="NLM_UMLS",
        ledger_tag="icd10cm_snomed",
        source_ontology="icd10cm",
        target_ontology="snomed_ct",
        source_col="ICD_CODE",
        target_col="SNOMED_CID",
        display_col="SNOMED_FSN",
        onetoone_col="IS_1-1MAP",
        normalise_source=_dot_icd10,
        description="NLM ICD-10-CM → SNOMED CT diagnostic map (tab-delimited)",
    ),
    "loinc-snomed": MapSpec(
        name="loinc-snomed",
        authority="LOINC_SNOMED",
        ledger_tag="loinc_snomed",
        source_ontology="loinc",
        target_ontology="snomed_ct",
        source_col="LOINC_NUM",
        target_col="SNOMED_CID",
        display_col="SNOMED_FSN",
        category_col="MAP_PRIORITY",
        description="LOINC → SNOMED CT observable/order map (tab-delimited)",
    ),
}


@dataclass(frozen=True)
class Pair:
    """One parsed source→target row, before it is matched to loaded concepts."""

    source_id: str
    target_id: str
    target_display: str
    confidence: float
    properties: dict[str, Any]


@dataclass
class LoadContext:
    """Shared state the stages read and extend, in order."""

    session: Session
    spec: MapSpec
    source_file: Path
    release: int | None = None  # detected from the filename if None
    force: bool = False
    started: float = field(default_factory=time.monotonic)
    # populated by stages:
    checksum: str = ""
    pairs: list[Pair] = field(default_factory=list)  # every parsed row
    edges: list[dict[str, Any]] = field(default_factory=list)  # matched → insertable
    counters: dict[str, int] = field(default_factory=dict)


class LoadStage(Protocol):
    """One pipeline stage: a name and a run() over the shared context."""

    name: ClassVar[str]

    def run(self, ctx: LoadContext) -> str:
        """Execute the stage; return a one-line summary for the report."""
        ...


def default_stages() -> tuple[LoadStage, ...]:
    """The crosswalk pipeline, in execution order."""
    from hdh.modules.ontology.crosswalk import stages as s

    return (
        s.AcquireStage(),
        s.ParseStage(),
        s.MatchStage(),
        s.LoadEdgesStage(),
        s.VerifyStage(),
        s.FinalizeStage(),
    )


def spec_for(source_file: Path, name: str | None) -> MapSpec:
    """Resolve the :class:`MapSpec`: the named one, or the one whose columns
    the file's header carries (so ``--map`` can be omitted)."""
    if name is not None:
        if name not in SPECS:
            raise LoadError(f"unknown map '{name}' — known: {', '.join(sorted(SPECS))}")
        return SPECS[name]
    try:
        # utf-8-sig strips a leading BOM: Windows PowerShell and Excel both
        # write one, and it would otherwise corrupt the first header cell.
        header = source_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()[0]
    except (OSError, IndexError) as err:
        raise LoadError(f"cannot read a header from {source_file}: {err}") from None
    fields = {h.strip() for h in header.split("\t")}
    matches = [s for s in SPECS.values() if {s.source_col, s.target_col} <= fields]
    if len(matches) != 1:
        known = ", ".join(sorted(SPECS))
        raise LoadError(
            f"cannot infer the map from the header of {source_file.name} "
            f"(matched {len(matches)}) — pass --map <{known}>"
        )
    return matches[0]


def run_load(
    session: Session,
    source_file: str | Path,
    map_name: str | None = None,
    release: int | None = None,
    force: bool = False,
    stages: tuple[LoadStage, ...] | None = None,
) -> list[tuple[str, str]]:
    """Run the pipeline; returns (stage name, summary) pairs in order."""
    path = Path(source_file)
    if not path.is_file():
        raise LoadError(f"no such map file: {path}")
    ctx = LoadContext(
        session=session, spec=spec_for(path, map_name), source_file=path, release=release, force=force
    )
    report = []
    for stage in stages if stages is not None else default_stages():
        report.append((stage.name, stage.run(ctx)))
    return report
