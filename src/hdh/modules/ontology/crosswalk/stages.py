"""The six crosswalk pipeline stages (issue #86, part 2).

acquire → parse → match → load-edges → verify → finalize. Parsing is pure and
DB-free; matching is the honest part — an official map lists pairs, but an
edge can only exist where *both* concepts are loaded (the `ontology_edges`
foreign keys require it), so a map loaded before its catalogs yields rows that
are reported and skipped rather than edges that reference nothing.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from typing import ClassVar

from sqlalchemy import delete, func, insert, select

from hdh.modules.ontology.crosswalk import LoadContext, LoadError, Pair

_RELEASE_RE = re.compile(r"(\d{8}|\d{6})")
_TRUE = {"1", "true", "t", "yes", "y"}


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_loads"]


class AcquireStage:
    """Stage 1: checksum the file and detect the release from its name."""

    name: ClassVar[str] = "acquire"

    def run(self, ctx: LoadContext) -> str:
        """Hash the source and fill the release tag (or demand one)."""
        ctx.checksum = hashlib.sha256(ctx.source_file.read_bytes()).hexdigest()[:16]
        if ctx.release is None:
            stamp = _RELEASE_RE.search(ctx.source_file.name)
            if stamp is None:
                raise LoadError(
                    f"cannot detect a release from '{ctx.source_file.name}' — pass --release YYYYMM"
                )
            ctx.release = int(stamp.group(1)[:6])
        return f"{ctx.spec.name}, release {ctx.release}, sha {ctx.checksum}"


class ParseStage:
    """Stage 2: read the delimited rows into :class:`Pair`s (no DB).

    One row → one candidate pair. Confidence comes from the one-to-one column
    when the spec names one — a one-to-many row is a weaker claim and says so.
    Blank codes are skipped; a header missing a required column is fatal,
    because silently producing zero pairs would read as "nothing mapped".
    """

    name: ClassVar[str] = "parse"

    def run(self, ctx: LoadContext) -> str:
        """Parse the delimited rows into candidate pairs; count the blanks."""
        spec = ctx.spec
        with ctx.source_file.open(encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=spec.delimiter)
            missing = {spec.source_col, spec.target_col} - set(reader.fieldnames or [])
            if missing:
                raise LoadError(
                    f"{ctx.source_file.name}: header is missing required column(s) {sorted(missing)}"
                )
            blank = 0
            for row in reader:
                source, target = (
                    (row.get(spec.source_col) or "").strip(),
                    (row.get(spec.target_col) or "").strip(),
                )
                if not source or not target:
                    blank += 1
                    continue
                ctx.pairs.append(self._pair(spec, row, source, target))
        ctx.counters["parsed"] = len(ctx.pairs)
        ctx.counters["blank"] = blank
        return f"{len(ctx.pairs):,} pairs parsed ({blank:,} blank rows skipped)"

    @staticmethod
    def _pair(spec, row, source: str, target: str) -> Pair:
        confidence = spec.default_confidence
        properties = {"map": spec.name}
        if spec.onetoone_col is not None:
            one_to_one = (row.get(spec.onetoone_col) or "").strip().lower() in _TRUE
            confidence = 1.0 if one_to_one else spec.many_confidence
            properties["one_to_one"] = one_to_one
        if spec.category_col is not None and row.get(spec.category_col):
            properties["category"] = row[spec.category_col].strip()
        return Pair(
            source_id=spec.source_id(source),
            target_id=spec.target_id(target),
            target_display=(row.get(spec.display_col) or "").strip() if spec.display_col else "",
            confidence=confidence,
            properties=properties,
        )


class MatchStage:
    """Stage 3: keep only pairs whose *both* concepts are loaded.

    The map is authoritative about the relationship; it is not authoritative
    about what is in this database. A pair whose SNOMED concept was never
    loaded (no licence, or a newer map than the catalog) cannot become an
    edge, and inventing a stub concept for it would smuggle licensed identity
    in through the loader. Such rows are counted by cause and skipped.
    """

    name: ClassVar[str] = "match"

    def run(self, ctx: LoadContext) -> str:
        """Resolve each pair against the loaded concepts; keep only matches."""
        concepts_t, _edges_t, _loads_t = _tables()
        spec = ctx.spec
        known = {
            row[0]
            for row in ctx.session.execute(
                select(concepts_t.c.id).where(
                    concepts_t.c.ontology.in_((spec.source_ontology, spec.target_ontology))
                )
            )
        }
        no_source = no_target = 0
        seen: set[tuple[str, str]] = set()
        for pair in ctx.pairs:
            source_ok, target_ok = pair.source_id in known, pair.target_id in known
            if not source_ok:
                no_source += 1
            if not target_ok:
                no_target += 1
            key = (pair.source_id, pair.target_id)
            if source_ok and target_ok and key not in seen:
                seen.add(key)
                ctx.edges.append(
                    {
                        "source_id": pair.source_id,
                        "target_id": pair.target_id,
                        "edge_type": "maps_to",
                        "authority": spec.authority,
                        "confidence": pair.confidence,
                        "properties": dict(pair.properties, display=pair.target_display)
                        if pair.target_display
                        else pair.properties,
                    }
                )
        ctx.counters.update(matched=len(ctx.edges), no_source=no_source, no_target=no_target)
        return (
            f"{len(ctx.edges):,} edges matched; skipped {no_source:,} with no "
            f"{spec.source_ontology} concept, {no_target:,} with no {spec.target_ontology} concept"
        )


class LoadEdgesStage:
    """Stage 4: replace this authority's edges wholesale — idempotent.

    Idempotency keys on the authority, never the ledger: `hdh snomed purge`
    removes these edges (they target `snomed_ct:`) but cannot reach this
    module's ledger, so a guard that trusted the ledger would refuse a reload
    after a purge. The presence of this authority's edges is the truth; absent
    ``--force`` a load that would add to an already-populated authority
    refuses rather than double it.
    """

    name: ClassVar[str] = "load-edges"

    def run(self, ctx: LoadContext) -> str:
        """Refuse an already-loaded authority without --force; then replace it."""
        _concepts_t, edges_t, _loads_t = _tables()
        existing = ctx.session.execute(
            select(func.count())
            .select_from(edges_t)
            .where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == ctx.spec.authority)
        ).scalar()
        if existing and not ctx.force:
            raise LoadError(
                f"{existing:,} '{ctx.spec.authority}' edges are already loaded — "
                "re-run with --force to replace them"
            )
        ctx.session.execute(
            delete(edges_t).where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == ctx.spec.authority)
        )
        if ctx.edges:
            ctx.session.execute(insert(edges_t), ctx.edges)
        ctx.session.flush()
        return f"{len(ctx.edges):,} '{ctx.spec.authority}' maps_to edges written ({existing:,} replaced)"


class VerifyStage:
    """Stage 5: invariants over what was just written."""

    name: ClassVar[str] = "verify"

    def run(self, ctx: LoadContext) -> str:
        """Check the written count matches and no edge points at a missing concept."""
        _concepts_t, edges_t, _loads_t = _tables()
        written = ctx.session.execute(
            select(func.count())
            .select_from(edges_t)
            .where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == ctx.spec.authority)
        ).scalar()
        if written != len(ctx.edges):
            raise LoadError(f"edge count drift: wrote {len(ctx.edges)}, table holds {written}")
        dangling = ctx.session.execute(
            select(func.count())
            .select_from(edges_t)
            .outerjoin(_concepts_t, edges_t.c.target_id == _concepts_t.c.id)
            .where(
                edges_t.c.edge_type == "maps_to",
                edges_t.c.authority == ctx.spec.authority,
                _concepts_t.c.id.is_(None),
            )
        ).scalar()
        if dangling:
            raise LoadError(f"{dangling} edges point at an absent target concept")
        return f"{written:,} edges, no dangling targets"


class FinalizeStage:
    """Stage 6: write the ledger row — only reached if every stage passed.

    The prior ledger row for this map is replaced, not appended, so the ledger
    carries exactly one row per loaded map and ``status`` never has to guess
    which is current.
    """

    name: ClassVar[str] = "finalize"

    def run(self, ctx: LoadContext) -> str:
        """Replace this map's ledger row with a fresh one and commit."""
        _concepts_t, _edges_t, loads_t = _tables()
        duration = time.monotonic() - ctx.started
        ctx.session.execute(delete(loads_t).where(loads_t.c.ontology == ctx.spec.ledger_tag))
        ctx.session.execute(
            insert(loads_t),
            [
                {
                    "ontology": ctx.spec.ledger_tag,
                    "fiscal_year": ctx.release,
                    "source_checksums": {ctx.source_file.name: ctx.checksum},
                    "concept_count": 0,  # a crosswalk adds edges, not concepts
                    "edge_count": ctx.counters.get("matched", 0),
                    "duration_seconds": round(duration, 2),
                    "properties": {
                        "map": ctx.spec.name,
                        "authority": ctx.spec.authority,
                        "parsed": ctx.counters.get("parsed", 0),
                        "skipped_no_source": ctx.counters.get("no_source", 0),
                        "skipped_no_target": ctx.counters.get("no_target", 0),
                    },
                }
            ],
        )
        ctx.session.commit()
        return f"{ctx.spec.ledger_tag} release {ctx.release} recorded ({duration:.2f}s)"
