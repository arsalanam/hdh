"""Load an RxNorm release into the shared ontology tables.

RxNorm is free but redistributable only under UMLS terms, so this module
ships the LOADER and never the data — the rule SNOMED and LOINC follow,
and the reason `just release-check` refuses to build an asset containing
licensed rows.

Three RRF files, pipe-delimited with a trailing separator and no header:

    RXNCONSO.RRF   atoms: RXCUI, TTY, STR — the concepts and their names
    RXNREL.RRF     typed edges between concepts
    RXNSAT.RRF     attributes: strength, and the rest

**RxNorm is a graph, not a tree**, and the term types are the whole point:
``Lisinopril`` (IN) and ``Lisinopril 10 MG Oral Tablet`` (SCD) are
different concepts at different levels, and coding a note at the wrong one
is the ordinary way to get medications wrong (design §4).

Two kinds of edge come out of it. The **specificity ladder** —
ingredient → component → clinical drug → branded drug — becomes
``parent_of``, so subsumption means what a reader expects: a branded
tablet IS-A clinical drug IS-A ingredient. Everything else (dose form,
precise ingredient) becomes an ``attribute`` edge carrying its RxNorm
relation name, the same way SNOMED's defining attributes do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ONTOLOGY = "rxnorm"

CONSO, REL, SAT = "RXNCONSO.RRF", "RXNREL.RRF", "RXNSAT.RRF"

#: RXNCONSO column positions we read (RRF has no header row).
_C_RXCUI, _C_LAT, _C_SAB, _C_TTY, _C_STR, _C_SUPPRESS = 0, 1, 11, 12, 14, 16
#: RXNREL: read as RXCUI2 --RELA--> RXCUI1, which is the release's own
#: reading ("the relationship of the second concept to the first").
_R_RXCUI1, _R_RXCUI2, _R_RELA, _R_SUPPRESS = 0, 4, 7, 14
#: RXNSAT
_S_RXCUI, _S_ATN, _S_ATV, _S_SUPPRESS = 0, 8, 10, 11

#: The term types worth a concept of their own. Everything else in a
#: release is either a source atom (which we keep as a TERM on the concept
#: it names) or a level we have no use for.
CONCEPT_TTYS = {
    "IN": "ingredient",
    "PIN": "precise ingredient",
    "MIN": "multiple ingredients",
    "BN": "brand name",
    "SCDC": "clinical drug component",
    "SCD": "clinical drug",
    "SBD": "branded drug",
    "DF": "dose form",
}

#: RELA values where the SOURCE is the more general concept, so the edge is
#: a rung on the specificity ladder. Their inverses are skipped rather than
#: stored twice.
LADDER_RELAS = {"ingredient_of", "constitutes", "has_tradename"}
_INVERSE_OF_LADDER = {"has_ingredient", "consists_of", "tradename_of"}

#: RELA values kept as typed attributes — what a drug is made of and how it
#: is taken, which is what the compositional walk (§5) filters on.
ATTRIBUTE_RELAS = {"has_dose_form", "has_precise_ingredient", "contains", "has_form"}

#: Attributes worth carrying on the concept.
KEPT_ATTRIBUTES = {"RXN_AVAILABLE_STRENGTH", "RXN_BOSS_STRENGTH_NUM_VALUE", "RXN_BOSS_STRENGTH_NUM_UNIT"}


class RxNormLoadError(Exception):
    """The release is not where we were told, or not what we expected."""


@dataclass(frozen=True)
class LoadReport:
    """What a load did — printed by the CLI, asserted by the tests."""

    concepts: int
    terms: int
    ladder_edges: int
    attribute_edges: int
    source: str

    def lines(self) -> tuple[str, ...]:
        return (
            f"source      {self.source}",
            f"concepts    {self.concepts:,}",
            f"terms       {self.terms:,}",
            f"ladder      {self.ladder_edges:,} parent_of edges",
            f"attributes  {self.attribute_edges:,} typed edges",
        )


def _find(source: Path, filename: str) -> Path | None:
    direct = source / filename
    if direct.exists():
        return direct
    return next(iter(sorted(source.rglob(filename))), None)


def _rows(path: Path):
    """RRF lines as column lists. The trailing pipe is real, so the last
    split element is always empty and is dropped."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line:
                yield line.split("|")


def run_load(session, source_dir: str | Path, batch: int = 5000) -> LoadReport:
    """Read a release directory and replace this ontology's rows."""
    from sqlalchemy import delete, insert

    from hdh.core.models import Base

    source = Path(source_dir)
    if not source.exists():
        raise RxNormLoadError(f"no such directory: {source}")
    conso_path = _find(source, CONSO)
    if conso_path is None:
        raise RxNormLoadError(
            f"{CONSO} not found under {source} — point --source at an unpacked RxNorm release"
        )

    concepts_t = Base.metadata.tables["ontology_concepts"]
    terms_t = Base.metadata.tables["ontology_terms"]
    edges_t = Base.metadata.tables["ontology_edges"]

    # Replace rather than merge: a release is a snapshot, and a half-old
    # catalog is worse than either version of it.
    session.execute(delete(edges_t).where(edges_t.c.source_id.like(f"{ONTOLOGY}:%")))
    session.execute(delete(terms_t).where(terms_t.c.concept_id.like(f"{ONTOLOGY}:%")))
    session.execute(delete(concepts_t).where(concepts_t.c.ontology == ONTOLOGY))

    concepts: dict[str, dict] = {}
    atoms: list[tuple[str, str, str, str]] = []  # rxcui, tty, sab, string
    for row in _rows(conso_path):
        if len(row) <= _C_SUPPRESS:
            continue
        rxcui, lat, sab = row[_C_RXCUI].strip(), row[_C_LAT].strip(), row[_C_SAB].strip()
        tty, string = row[_C_TTY].strip(), row[_C_STR].strip()
        if not rxcui or not string or lat != "ENG":
            continue
        if row[_C_SUPPRESS].strip().upper() not in ("", "N"):
            continue  # a withdrawn atom must not stay searchable
        atoms.append((rxcui, tty, sab, string))
        if sab == "RXNORM" and tty in CONCEPT_TTYS and rxcui not in concepts:
            concepts[rxcui] = {
                "id": f"{ONTOLOGY}:{rxcui}",
                "ontology": ONTOLOGY,
                "code": rxcui,
                "kind": "concept",
                "display": string[:512],
                "properties": {"tty": tty, "level": CONCEPT_TTYS[tty]},
            }

    if not concepts:
        raise RxNormLoadError(f"{conso_path} produced no RXNORM concepts — is this an RxNorm release?")

    # Attributes travel on the concept, so they are read before the insert.
    sat_path = _find(source, SAT)
    if sat_path is not None:
        for row in _rows(sat_path):
            if len(row) <= _S_SUPPRESS:
                continue
            rxcui, atn = row[_S_RXCUI].strip(), row[_S_ATN].strip()
            if atn in KEPT_ATTRIBUTES and rxcui in concepts:
                concepts[rxcui]["properties"][atn.lower()] = row[_S_ATV].strip()

    session.execute(insert(concepts_t), list(concepts.values()))

    # Terms: every English atom naming a concept we kept. Source atoms are
    # where a funnel's recall actually comes from — they carry the names
    # clinicians write rather than the normalized form.
    term_rows, seen = [], set()
    for rxcui, tty, sab, string in atoms:
        if rxcui not in concepts:
            continue
        key = (rxcui, string.lower())
        if key in seen:
            continue
        seen.add(key)
        preferred = sab == "RXNORM" and tty == concepts[rxcui]["properties"]["tty"]
        term_rows.append(
            {
                "concept_id": f"{ONTOLOGY}:{rxcui}",
                "term": string[:512],
                "term_type": "preferred" if preferred else "synonym",
                "language": "en",
                "active": True,
                "properties": {"tty": tty, "sab": sab},
            }
        )
    for start in range(0, len(term_rows), batch):
        session.execute(insert(terms_t), term_rows[start : start + batch])

    ladder, attributes = _read_relations(_find(source, REL), concepts)
    for rows in (ladder, attributes):
        for start in range(0, len(rows), batch):
            session.execute(insert(edges_t), rows[start : start + batch])

    session.commit()
    return LoadReport(
        concepts=len(concepts),
        terms=len(term_rows),
        ladder_edges=len(ladder),
        attribute_edges=len(attributes),
        source=str(conso_path.parent),
    )


def _read_relations(rel_path: Path | None, concepts: dict) -> tuple[list[dict], list[dict]]:
    """(ladder edges, attribute edges) — both deduplicated.

    The ladder is what ``ancestors`` and ``subsumes`` walk, so a duplicate
    there costs a wrong count rather than just a wasted row.
    """
    if rel_path is None:
        return [], []
    ladder: dict[tuple[str, str], dict] = {}
    attributes: dict[tuple[str, str, str], dict] = {}
    for row in _rows(rel_path):
        if len(row) <= _R_SUPPRESS:
            continue
        if row[_R_SUPPRESS].strip().upper() not in ("", "N"):
            continue
        rela = row[_R_RELA].strip()
        # read as RXCUI2 --RELA--> RXCUI1
        source, target = row[_R_RXCUI2].strip(), row[_R_RXCUI1].strip()
        if source not in concepts or target not in concepts or source == target:
            continue
        if rela in _INVERSE_OF_LADDER:
            continue  # the same rung, stored once from the general end
        if rela in LADDER_RELAS:
            ladder[(source, target)] = {
                "source_id": f"{ONTOLOGY}:{source}",
                "target_id": f"{ONTOLOGY}:{target}",
                "edge_type": "parent_of",
                "authority": "RXNORM",
                "confidence": 1.0,
                "properties": {"rela": rela},
            }
        elif rela in ATTRIBUTE_RELAS:
            attributes[(source, target, rela)] = {
                "source_id": f"{ONTOLOGY}:{source}",
                "target_id": f"{ONTOLOGY}:{target}",
                "edge_type": "attribute",
                "authority": "RXNORM",
                "confidence": 1.0,
                "properties": {"rela": rela},
            }
    return list(ladder.values()), list(attributes.values())
