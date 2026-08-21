"""Load a LOINC release into the shared ontology tables.

LOINC is licensed. Regenstrief distributes it free of charge but behind an
account and a licence, so this module ships the LOADER and never the data
— the same rule SNOMED CT follows, and the same reason `just release-check`
refuses to build an asset containing licensed rows.

What a release gives us, and what we take from it:

    LoincTable/Loinc.csv                         one row per code
    AccessoryFiles/MultiAxialHierarchy/…csv      the tree (optional)

``Loinc.csv`` is unusually generous about naming: besides the long common
name it carries a short name, a display name and ``RELATEDNAMES2``, a
semicolon-separated list of the things clinicians actually call the test.
That last column is the reason the funnel works at all for labs — "A1c"
and "sugar test" are in there, and the alternative would have been hand
curation (issue #54).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

ONTOLOGY = "loinc"

#: Where the two files live inside an unpacked release. Both are searched
#: recursively too, because Regenstrief has moved them between versions.
LOINC_TABLE = "Loinc.csv"
HIERARCHY_TABLE = "MultiAxialHierarchy.csv"

#: LOINC statuses we load. DISCOURAGED and DEPRECATED codes stay OUT of
#: the catalog: a coder must not quietly assign a retired code, and a
#: chart that contains one cannot say whether it was current when written.
ACTIVE_STATUSES = frozenset({"ACTIVE", "TRIAL"})

#: The six axes that define a LOINC code. Kept in `properties` so the
#: funnel can prefer, say, a serum sodium over a urine one without this
#: module having to grow clinical opinions.
AXES = (
    ("COMPONENT", "component"),
    ("PROPERTY", "property"),
    ("TIME_ASPCT", "time"),
    ("SYSTEM", "system"),
    ("SCALE_TYP", "scale"),
    ("METHOD_TYP", "method"),
)


class LoincLoadError(Exception):
    """The release is not where we were told, or not what we expected."""


@dataclass(frozen=True)
class LoadReport:
    """What a load did — printed by the CLI, asserted by the tests."""

    concepts: int
    terms: int
    hierarchy_rows: int
    source: str

    def lines(self) -> tuple[str, ...]:
        tree = f"{self.hierarchy_rows} hierarchy paths" if self.hierarchy_rows else "no hierarchy file"
        return (
            f"source     {self.source}",
            f"concepts   {self.concepts:,}",
            f"terms      {self.terms:,}",
            f"hierarchy  {tree}",
        )


def _find(source: Path, filename: str) -> Path | None:
    direct = source / filename
    if direct.exists():
        return direct
    return next(iter(sorted(source.rglob(filename))), None)


def _terms_for(row: dict) -> list[tuple[str, str]]:
    """(term, term_type) pairs, preferred first, de-duplicated.

    RELATEDNAMES2 is where the clinician's vocabulary lives; without it a
    lab funnel is matching against nothing but formal names.
    """
    seen: dict[str, str] = {}
    for value in (row.get("LONG_COMMON_NAME"), row.get("SHORTNAME"), row.get("DisplayName")):
        text = (value or "").strip()
        if text:
            seen.setdefault(text.lower(), text)
    related = (row.get("RELATEDNAMES2") or "").split(";")
    for text in related:
        cleaned = text.strip()
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)

    preferred = (row.get("LONG_COMMON_NAME") or "").strip()
    out = []
    for text in seen.values():
        out.append((text, "preferred" if text == preferred else "synonym"))
    out.sort(key=lambda pair: (pair[1] != "preferred", pair[0]))
    return out


def _read_hierarchy(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(code → dotted PATH_TO_ROOT, code → its label).

    LOINC ships a materialized path rather than a parent table, which is
    the same shape ICD-10-CM uses — so the tree costs a column, not a
    closure build (design icd10cm §5).

    The labels matter because the INTERIOR nodes of LOINC's tree are Parts
    ("LP7789-2", Chemistry) and Parts do not appear in ``Loinc.csv`` at
    all. Load only the numbered codes and every ancestor lookup comes back
    empty — the tree is there, and nothing is standing on it.
    """
    paths: dict[str, str] = {}
    labels: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("CODE") or "").strip()
            route = (row.get("PATH_TO_ROOT") or "").strip()
            if not code or not route:
                continue
            paths.setdefault(code, route)
            label = (row.get("CODE_TEXT") or "").strip()
            if label:
                labels.setdefault(code, label)
    return paths, labels


def run_load(session, source_dir: str | Path, batch: int = 5000) -> LoadReport:
    """Read a release directory and replace this ontology's rows."""
    from sqlalchemy import delete, insert, select

    from hdh.core.models import Base

    source = Path(source_dir)
    if not source.exists():
        raise LoincLoadError(f"no such directory: {source}")
    table_path = _find(source, LOINC_TABLE)
    if table_path is None:
        raise LoincLoadError(
            f"{LOINC_TABLE} not found under {source} — point --source at an unpacked LOINC release"
        )
    hierarchy_path = _find(source, HIERARCHY_TABLE)
    paths, labels = _read_hierarchy(hierarchy_path) if hierarchy_path else ({}, {})

    concepts_t = Base.metadata.tables["ontology_concepts"]
    terms_t = Base.metadata.tables["ontology_terms"]

    # Replace rather than merge: a release is a snapshot, and a half-old
    # catalog is worse than either version of it.
    session.execute(delete(terms_t).where(terms_t.c.concept_id.like(f"{ONTOLOGY}:%")))
    session.execute(delete(concepts_t).where(concepts_t.c.ontology == ONTOLOGY))

    concept_rows: list[dict] = []
    term_rows: list[dict] = []
    concepts = terms = 0

    def flush() -> None:
        nonlocal concept_rows, term_rows
        if concept_rows:
            session.execute(insert(concepts_t), concept_rows)
            concept_rows = []
        if term_rows:
            session.execute(insert(terms_t), term_rows)
            term_rows = []

    with table_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("LOINC_NUM") or "").strip()
            status = (row.get("STATUS") or "ACTIVE").strip().upper()
            if not code or status not in ACTIVE_STATUSES:
                continue
            concept_id = f"{ONTOLOGY}:{code}"
            route = paths.get(code)
            display = (row.get("LONG_COMMON_NAME") or row.get("SHORTNAME") or code).strip()
            concept_rows.append(
                {
                    "id": concept_id,
                    "ontology": ONTOLOGY,
                    "code": code,
                    "kind": "concept",
                    "display": display[:512],
                    "short_display": ((row.get("SHORTNAME") or "").strip() or None),
                    "path": route,
                    "hierarchy_depth": route.count(".") if route else None,
                    "properties": {
                        **{key: (row.get(column) or "").strip() for column, key in AXES},
                        "class": (row.get("CLASS") or "").strip(),
                        "status": status,
                    },
                }
            )
            concepts += 1
            for text, term_type in _terms_for(row):
                term_rows.append(
                    {
                        "concept_id": concept_id,
                        "term": text[:512],
                        "term_type": term_type,
                        "language": "en",
                        "active": True,
                        "properties": {},
                    }
                )
                terms += 1
            if len(concept_rows) >= batch:
                flush()
    flush()

    # The tree's interior: LOINC Parts, which carry the hierarchy but are
    # absent from Loinc.csv. Without them `ancestors()` has nothing to
    # return and `descendants()` has no node to start from.
    numbered = {
        code
        for (code,) in session.execute(
            select(concepts_t.c.code).where(concepts_t.c.ontology == ONTOLOGY)
        ).all()
    }
    part_rows = []
    for code, route in paths.items():
        if code in numbered:
            continue
        part_rows.append(
            {
                "id": f"{ONTOLOGY}:{code}",
                "ontology": ONTOLOGY,
                "code": code,
                "kind": "category",  # an interior node, not an orderable test
                "display": (labels.get(code) or code)[:512],
                "path": route,
                "hierarchy_depth": route.count("."),
                "properties": {"part": True},
            }
        )
    if part_rows:
        session.execute(insert(concepts_t), part_rows)
        concepts += len(part_rows)
    session.commit()
    if concepts == 0:
        raise LoincLoadError(f"{table_path} produced no active concepts — is this a LOINC release?")
    return LoadReport(
        concepts=concepts, terms=terms, hierarchy_rows=len(paths), source=str(table_path.parent)
    )
