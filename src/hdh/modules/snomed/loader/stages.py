"""The eight pipeline stages (design snomed-module.md §4).

Meaning lives here: preferred-term resolution through the US English
language refset, is-a inversion to ``parent_of`` edges, defining
attributes to generic ``attribute`` edges, and the in-memory transitive
closure. ``path``/``hierarchy_depth`` stay NULL by contract — SNOMED is
a DAG; the closure table is this module's private hierarchy strategy.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from typing import Any, ClassVar

from sqlalchemy import delete, func, insert, select

from hdh.modules.snomed.loader import ONTOLOGY, LoadContext, LoadError
from hdh.modules.snomed.loader.rf2 import (
    ATTRIBUTE_NAMES,
    FSN_TYPE,
    IS_A,
    PREFERRED,
    ROOT_CONCEPT,
    US_ENGLISH_REFSET,
    Rf2Error,
    find_rf2_files,
    is_valid_sctid,
    iter_rows,
)

BATCH = 5_000
_SEMANTIC_TAG_RE = re.compile(r"\(([^()]+)\)\s*$")
_RELEASE_RE = re.compile(r"_(\d{8})\.txt$")


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return (
        t["ontology_concepts"],
        t["ontology_edges"],
        t["ontology_terms"],
        t["ontology_closure"],
        t["ontology_loads"],
    )


def _cid(sctid: str) -> str:
    return f"{ONTOLOGY}:{sctid}"


def _bulk_insert(session, table, rows: list[dict[str, Any]]) -> None:
    """COPY-batched insert on PostgreSQL (psycopg3), executemany elsewhere.

    The design's "COPY-batched inserts" (§4 stage 4): at US Edition scale
    (~1.6M terms, ~10M closure rows) COPY is an order of magnitude faster
    than multi-VALUES INSERT. JSON columns are pre-serialized; PostgreSQL
    parses them through the jsonb input function."""
    if not rows:
        return
    bind = session.get_bind()
    if bind.dialect.name == "postgresql" and bind.dialect.driver == "psycopg":
        import json

        columns = [c.name for c in table.columns if c.name in rows[0]]
        json_columns = {c.name for c in table.columns if "JSON" in type(c.type).__name__.upper()}
        driver_connection = session.connection().connection.driver_connection
        column_list = ", ".join(columns)
        with driver_connection.cursor() as cursor:
            with cursor.copy(f"COPY {table.name} ({column_list}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(
                        tuple(
                            json.dumps(row.get(col))
                            if col in json_columns and row.get(col) is not None
                            else row.get(col)
                            for col in columns
                        )
                    )
        return
    for i in range(0, len(rows), BATCH):
        session.execute(insert(table), rows[i : i + BATCH])


class AcquireStage:
    """Stage 1: locate the RF2 files, checksum them, detect the release."""

    name: ClassVar[str] = "acquire"

    def run(self, ctx: LoadContext) -> str:
        """Find the four Snapshot files; fill checksums and the release tag."""
        try:
            ctx.files = find_rf2_files(ctx.source_dir)
        except Rf2Error as err:
            raise LoadError(str(err)) from None
        for key, path in ctx.files.items():
            ctx.checksums[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if ctx.release is None:
            stamp = _RELEASE_RE.search(ctx.files["concepts"].name)
            if stamp is None:
                raise LoadError(
                    f"cannot detect release from '{ctx.files['concepts'].name}' — pass --release YYYYMM"
                )
            ctx.release = int(stamp.group(1)[:6])
        return f"4 RF2 snapshot files, release {ctx.release}"


class ParseStage:
    """Stage 2: a streaming validation pass — headers, row shapes, SCTIDs.

    Nothing is materialized here: at US Edition scale the description and
    relationship files alone are millions of rows, so the build stage
    re-streams each file exactly once more instead of holding raw dicts."""

    name: ClassVar[str] = "parse"

    def run(self, ctx: LoadContext) -> str:
        """Stream all four files; validate structure without storing rows."""
        counts: dict[str, int] = {}
        bad: list[str] = []
        try:
            for key, path in ctx.files.items():
                n = 0
                for row in iter_rows(path):
                    n += 1
                    if key == "concepts" and not is_valid_sctid(row["id"]) and len(bad) < 5:
                        bad.append(row["id"])
                counts[key] = n
        except Rf2Error as err:
            raise LoadError(str(err)) from None
        if bad:
            raise LoadError(f"invalid SCTIDs in concept file (check digit): {bad}")
        ctx.counters.update({f"rows_{key}": n for key, n in counts.items()})
        return ", ".join(
            f"{counts[k]:,} {k}" for k in ("concepts", "descriptions", "relationships", "language")
        )


class BuildStage:
    """Stage 3: active concepts + preferred terms + semantic tags; term
    rows; edge rows (is-a → parent_of, everything else → attribute).

    Each RF2 file is streamed exactly once (US Edition: millions of
    description/relationship rows) — only the built, insertable rows are
    held, never the raw file dicts."""

    name: ClassVar[str] = "build"

    def run(self, ctx: LoadContext) -> str:
        """Resolve meaning from the streamed rows into insertable dicts."""
        preferred_marks = {
            row["referencedComponentId"]
            for row in iter_rows(ctx.files["language"])
            if row["active"] == "1"
            and row["refsetId"] == US_ENGLISH_REFSET
            and row["acceptabilityId"] == PREFERRED
        }
        for row in iter_rows(ctx.files["concepts"]):
            if row["active"] != "1":
                continue
            sctid = row["id"]
            ctx.concepts[_cid(sctid)] = {
                "id": _cid(sctid),
                "ontology": ONTOLOGY,
                "code": sctid,
                "kind": "concept",
                "display": sctid,  # placeholder until descriptions stream
                "effective_fy": ctx.release,
                "properties": {
                    "fsn": "",
                    "semantic_tag": None,
                    "effective_time": row["effectiveTime"],
                    "primitive": row["definitionStatusId"] == "900000000000074008",
                },
            }
        self._build_terms_and_names(ctx, preferred_marks)
        self._build_edges(ctx)
        return (
            f"{len(ctx.concepts):,} active concepts, {len(ctx.terms):,} terms, "
            f"{len(ctx.edges):,} edges ({sum(len(p) for p in ctx.parents.values()):,} is-a)"
        )

    @staticmethod
    def _build_terms_and_names(ctx: LoadContext, preferred_marks: set) -> None:
        """One descriptions pass: term rows + FSN/preferred-term resolution."""
        fsn: dict[str, str] = {}
        display: dict[str, str] = {}
        for row in iter_rows(ctx.files["descriptions"]):
            concept = ctx.concepts.get(_cid(row["conceptId"]))
            if row["active"] != "1" or concept is None:
                continue
            preferred = row["id"] in preferred_marks
            if row["typeId"] == FSN_TYPE:
                term_type = "fsn"
                if preferred:
                    fsn[row["conceptId"]] = row["term"]
            elif preferred:
                term_type = "preferred"
                display[row["conceptId"]] = row["term"]
            else:
                term_type = "synonym"
            ctx.terms.append(
                {
                    "concept_id": _cid(row["conceptId"]),
                    "term": row["term"],
                    "term_type": term_type,
                    "language": row["languageCode"],
                    "active": True,
                    "properties": {"description_id": row["id"]},
                }
            )
        for sctid, concept_fsn in fsn.items():
            concept = ctx.concepts[_cid(sctid)]
            tag_match = _SEMANTIC_TAG_RE.search(concept_fsn)
            concept["properties"]["fsn"] = concept_fsn
            concept["properties"]["semantic_tag"] = tag_match.group(1) if tag_match else None
            concept["display"] = concept_fsn
        for sctid, term in display.items():
            ctx.concepts[_cid(sctid)]["display"] = term

    @staticmethod
    def _build_edges(ctx: LoadContext) -> None:
        """is-a inverts to parent_of (parent → child); every other active
        relationship becomes one generic attribute edge (design §3)."""
        for row in iter_rows(ctx.files["relationships"]):
            if row["active"] != "1":
                continue
            if _cid(row["sourceId"]) not in ctx.concepts or _cid(row["destinationId"]) not in ctx.concepts:
                continue
            if row["typeId"] == IS_A:
                ctx.parents.setdefault(row["sourceId"], []).append(row["destinationId"])
                ctx.edges.append(
                    {
                        "source_id": _cid(row["destinationId"]),
                        "target_id": _cid(row["sourceId"]),
                        "edge_type": "parent_of",
                        "authority": "SNOMED_RF2",
                        "confidence": 1.0,
                        "properties": {},
                    }
                )
            else:
                type_concept = ctx.concepts.get(_cid(row["typeId"]))
                name = ATTRIBUTE_NAMES.get(row["typeId"]) or (
                    re.sub(r"[^a-z0-9]+", "_", type_concept["display"].lower()).strip("_")
                    if type_concept
                    else f"attribute_{row['typeId']}"
                )
                ctx.edges.append(
                    {
                        "source_id": _cid(row["sourceId"]),
                        "target_id": _cid(row["destinationId"]),
                        "edge_type": "attribute",
                        "authority": "SNOMED_RF2",
                        "confidence": 1.0,
                        "properties": {
                            "attribute": {"type_id": row["typeId"], "name": name},
                            "group": int(row["relationshipGroup"]),
                        },
                    }
                )


class LoadRowsStage:
    """Stage 4: bulk-insert concepts, terms, edges (ledger idempotency guard)."""

    name: ClassVar[str] = "load"

    def run(self, ctx: LoadContext) -> str:
        """Refuse a loaded release without --force; then batched inserts."""
        concepts_t, edges_t, terms_t, closure_t, loads_t = _tables()
        existing = ctx.session.execute(
            select(func.count())
            .select_from(loads_t)
            .where(loads_t.c.ontology == ONTOLOGY, loads_t.c.fiscal_year == ctx.release)
        ).scalar()
        if existing and not ctx.force:
            raise LoadError(f"release {ctx.release} is already loaded — re-run with --force to replace it")
        if ctx.force:
            prefix = f"{ONTOLOGY}:%"
            ctx.session.execute(delete(closure_t).where(closure_t.c.ancestor_id.like(prefix)))
            ctx.session.execute(delete(terms_t).where(terms_t.c.concept_id.like(prefix)))
            ctx.session.execute(
                delete(edges_t).where(edges_t.c.source_id.like(prefix) | edges_t.c.target_id.like(prefix))
            )
            ctx.session.execute(delete(concepts_t).where(concepts_t.c.ontology == ONTOLOGY))
        ctx.counters["concepts"] = len(ctx.concepts)
        ctx.counters["terms"] = len(ctx.terms)
        ctx.counters["edges"] = len(ctx.edges)
        _bulk_insert(ctx.session, concepts_t, list(ctx.concepts.values()))
        _bulk_insert(ctx.session, terms_t, ctx.terms)
        _bulk_insert(ctx.session, edges_t, ctx.edges)
        ctx.session.flush()
        ctx.terms = []  # free before the closure builds its 10M rows
        ctx.edges = []
        return (
            f"{ctx.counters['concepts']:,} concepts, {ctx.counters['terms']:,} terms, "
            f"{ctx.counters['edges']:,} edges inserted"
        )


class ClosureStage:
    """Stage 5: in-memory is-a transitive closure → bulk load (design §6).

    Per-node upward BFS over the parent adjacency: SNOMED ancestor sets
    are shallow (tens, not thousands), so this is O(concepts × ancestors)
    with min-depth semantics for free. The closure excludes self-pairs;
    ``subsumes`` is strict, like ICD's prefix test."""

    name: ClassVar[str] = "closure"

    def run(self, ctx: LoadContext) -> str:
        """Compute and insert (ancestor, descendant, min_depth) rows,
        flushing in batches so the full-scale ~10M-row closure never sits
        in memory all at once."""
        _c, _e, _t, closure_t, _l = _tables()
        pending: list[dict[str, Any]] = []
        total = 0
        max_depth = 0
        for sctid in (c["code"] for c in ctx.concepts.values()):
            depths: dict[str, int] = {}
            queue = deque((parent, 1) for parent in ctx.parents.get(sctid, ()))
            while queue:
                node, depth = queue.popleft()
                known = depths.get(node)
                if known is not None and known <= depth:
                    continue
                depths[node] = depth
                for parent in ctx.parents.get(node, ()):
                    queue.append((parent, depth + 1))
            for ancestor, depth in depths.items():
                pending.append(
                    {"ancestor_id": _cid(ancestor), "descendant_id": _cid(sctid), "min_depth": depth}
                )
                max_depth = max(max_depth, depth)
            if len(pending) >= BATCH * 10:
                _bulk_insert(ctx.session, closure_t, pending)
                total += len(pending)
                pending = []
        _bulk_insert(ctx.session, closure_t, pending)
        total += len(pending)
        ctx.session.flush()
        ctx.counters["closure"] = total
        return f"{total:,} closure rows (max depth {max_depth})"


class AccelerateStage:
    """Stage 6: PostgreSQL search accelerators over the terms index and a
    partial index for hot attribute lookups. No-op elsewhere; all DDL is
    static and idempotent."""

    name: ClassVar[str] = "accelerate"

    DDL = (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS ix_term_fts ON ontology_terms USING GIN (to_tsvector('english', term))",
        "CREATE INDEX IF NOT EXISTS ix_term_trgm ON ontology_terms USING GIN (term gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_edge_attribute_name ON ontology_edges "
        "(((properties -> 'attribute') ->> 'name')) WHERE edge_type = 'attribute'",
    )

    def run(self, ctx: LoadContext) -> str:
        """Create the PostgreSQL indexes (skips other dialects)."""
        from sqlalchemy import text

        bind = ctx.session.get_bind()
        if bind.dialect.name != "postgresql":
            return f"skipped ({bind.dialect.name})"
        for statement in self.DDL:
            ctx.session.execute(text(statement))
        ctx.session.flush()
        return f"{len(self.DDL) - 1} indexes ensured (+pg_trgm)"


class VerifyStage:
    """Stage 7: invariants over what was just written (design §4)."""

    name: ClassVar[str] = "verify"

    def run(self, ctx: LoadContext) -> str:
        """Check post-load invariants; raise on any violation."""
        return "; ".join(
            [
                self._root_present(ctx),
                self._every_concept_named(ctx),
                self._closure_consistent(ctx),
            ]
        )

    @staticmethod
    def _root_present(ctx: LoadContext) -> str:
        if _cid(ROOT_CONCEPT) not in ctx.concepts:
            raise LoadError(f"root concept {ROOT_CONCEPT} missing from the load")
        return "root present"

    @staticmethod
    def _every_concept_named(ctx: LoadContext) -> str:
        nameless = [
            c["code"]
            for c in ctx.concepts.values()
            if not c["properties"]["fsn"] or c["display"] == c["code"]
        ]
        if nameless:
            raise LoadError(f"concepts without FSN or preferred term: {nameless[:5]}")
        return f"{len(ctx.concepts):,} concepts named"

    @staticmethod
    def _closure_consistent(ctx: LoadContext) -> str:
        """Every direct is-a pair must be a depth-1 closure row, and only
        the root may have no ancestors (termination already proved
        acyclicity — a cycle would never converge)."""
        _c, _e, _t, closure_t, _l = _tables()
        depth1 = {
            (row.ancestor_id, row.descendant_id)
            for row in ctx.session.execute(select(closure_t).where(closure_t.c.min_depth == 1))
        }
        for child, parents in ctx.parents.items():
            for parent in parents:
                if (_cid(parent), _cid(child)) not in depth1:
                    raise LoadError(f"is-a pair missing from closure at depth 1: {parent} -> {child}")
        with_ancestors = int(
            ctx.session.execute(
                select(func.count()).select_from(
                    select(closure_t.c.descendant_id).group_by(closure_t.c.descendant_id).subquery()
                )
            ).scalar()
            or 0
        )
        expected = len(ctx.concepts) - 1  # everyone but the root descends from something
        if with_ancestors != expected:
            raise LoadError(f"{expected - with_ancestors} non-root concepts have no ancestors in the closure")
        return f"closure consistent ({ctx.counters['closure']:,} rows)"


class FinalizeStage:
    """Stage 8: write the ledger row — only reached if every stage passed."""

    name: ClassVar[str] = "finalize"

    def run(self, ctx: LoadContext) -> str:
        """Write the OntologyLoad ledger row and commit."""
        import time

        _c, _e, _t, _cl, loads_t = _tables()
        duration = time.monotonic() - ctx.started
        ctx.session.execute(
            insert(loads_t),
            [
                {
                    "ontology": ONTOLOGY,
                    "fiscal_year": ctx.release,
                    "source_checksums": ctx.checksums,
                    "concept_count": ctx.counters.get("concepts", 0),
                    "edge_count": ctx.counters.get("edges", 0),
                    "duration_seconds": round(duration, 2),
                    "properties": {
                        "terms": ctx.counters.get("terms", 0),
                        "closure_rows": ctx.counters.get("closure", 0),
                        "stages": "milestone-b-source-dir",
                    },
                }
            ],
        )
        ctx.session.commit()
        return f"release {ctx.release} recorded ({duration:.1f}s)"
