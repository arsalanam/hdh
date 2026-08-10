"""The milestone-B load stages (design §4.2, stages 1–2 and 4–9).

Laterality and the displacement axis are derived from the official long
descriptions by side-token normalization — never from character positions
(design §2, departure 2). Hierarchy is derived from code-prefix nesting
under the static chapter table; the block level joins in milestone C.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, ClassVar

from sqlalchemy import delete, func, insert, select

from hdh.modules.icd10cm.chapters import chapter_for
from hdh.modules.icd10cm.loader import CodeRow, LoadContext, LoadError

ONTOLOGY = "icd10cm"
BATCH = 2000

# 7th-character episode letters (order-file families in scope; the tabular
# XML's per-family seventh-character definitions replace this in milestone C)
EPISODE_CHARS = frozenset("ABCDEFGHJKMNPQRS")

_SIDE_WORDS = {"right": "1", "left": "2", "unspecified": "9"}
_SIDE_RE = re.compile(r"\b(right|left|unspecified)\b", re.IGNORECASE)
_DISPLACEMENT_RE = re.compile(r"\b(nondisplaced|displaced)\b", re.IGNORECASE)


def _tables():
    """The registry-materialized tables (resolved lazily, after bootstrap)."""
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_loads"]


class AcquireStage:
    """Stage 1: locate the source files and record their checksums."""

    name: ClassVar[str] = "acquire"

    def run(self, ctx: LoadContext) -> str:
        """Locate the release files and record their checksums."""
        pattern = f"icd10cm-order-{ctx.fiscal_year}*"
        matches = sorted(ctx.source_dir.glob(pattern + ".txt")) or sorted(ctx.source_dir.glob(pattern))
        if not matches:
            raise LoadError(
                f"no order file matching '{pattern}' in {ctx.source_dir} — "
                "download the CMS release files there first"
            )
        order_file = matches[0]
        ctx.files["order"] = order_file
        ctx.checksums[order_file.name] = hashlib.sha256(order_file.read_bytes()).hexdigest()
        return f"{order_file.name} ({order_file.stat().st_size:,} bytes)"


class ParseStage:
    """Stage 2: fixed-width order-file rows → CodeRow stream."""

    name: ClassVar[str] = "parse"

    def run(self, ctx: LoadContext) -> str:
        """Parse fixed-width order-file rows into CodeRows."""
        for lineno, line in enumerate(ctx.files["order"].read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                code = line[6:13].strip()
                row = CodeRow(
                    order=int(line[0:5]),
                    code=code,
                    dotted=code if len(code) <= 3 else f"{code[:3]}.{code[3:]}",
                    billable=line[14] == "1",
                    short=line[16:76].strip(),
                    long=line[77:].strip(),
                )
            except (ValueError, IndexError):
                raise LoadError(f"{ctx.files['order'].name}:{lineno}: unparseable row") from None
            if not row.code or not row.long:
                raise LoadError(f"{ctx.files['order'].name}:{lineno}: empty code or description")
            ctx.rows.append(row)
        billable = sum(r.billable for r in ctx.rows)
        return f"{len(ctx.rows):,} rows ({billable:,} billable)"


class StructureStage:
    """Stage 3 (order-file half): chapters + prefix-nesting hierarchy."""

    name: ClassVar[str] = "structure"

    def run(self, ctx: LoadContext) -> str:
        """Derive chapters and the prefix-nesting hierarchy."""
        by_code = {r.code: r for r in ctx.rows}
        chapters_used: dict[str, Any] = {}
        for row in ctx.rows:
            chapter = chapter_for(row.code[:3])
            if chapter is None:
                raise LoadError(f"{row.dotted}: no chapter covers category {row.code[:3]}")
            chapters_used[chapter.concept_id] = chapter
            parent_code = self._parent_code(row.code, by_code)
            path, depth = self._path(row.code, parent_code, chapter, ctx.concepts)
            ctx.concepts[f"{ONTOLOGY}:{row.dotted}"] = {
                "id": f"{ONTOLOGY}:{row.dotted}",
                "ontology": ONTOLOGY,
                "code": row.dotted,
                "kind": self._kind(row),
                "display": row.long,
                "short_display": row.short[:128],
                "is_billable": row.billable,
                "hierarchy_depth": depth,
                "path": path,
                "properties": {},
                "effective_fy": ctx.fiscal_year,
                "_parent": (
                    f"{ONTOLOGY}:{by_code[parent_code].dotted}" if parent_code else chapter.concept_id
                ),
            }
        for chapter in chapters_used.values():
            ctx.concepts[chapter.concept_id] = {
                "id": chapter.concept_id,
                "ontology": ONTOLOGY,
                "code": chapter.range_code,
                "kind": "chapter",
                "display": chapter.title,
                "short_display": chapter.title[:128],
                "is_billable": False,
                "hierarchy_depth": 0,
                "path": chapter.path_segment,
                "properties": {},
                "effective_fy": ctx.fiscal_year,
                "_parent": None,
            }
        return f"{len(ctx.concepts):,} concepts across {len(chapters_used)} chapters"

    @staticmethod
    def _parent_code(code: str, by_code: dict[str, CodeRow]) -> str | None:
        for length in range(len(code) - 1, 2, -1):
            if code[:length] in by_code:
                return code[:length]
        return None

    @staticmethod
    def _path(code: str, parent_code: str | None, chapter, concepts: dict) -> tuple[str, int]:
        if parent_code:
            parent = concepts[
                f"{ONTOLOGY}:{parent_code if len(parent_code) <= 3 else parent_code[:3] + '.' + parent_code[3:]}"
            ]
            return f"{parent['path']}.{code}", parent["hierarchy_depth"] + 1
        return f"{chapter.path_segment}.{code}", 1

    @staticmethod
    def _kind(row: CodeRow) -> str:
        if row.billable:
            return "code"
        return "category" if len(row.code) == 3 else "subcategory"


class EnrichStage:
    """Stage 4: semantic axes from descriptions (design §3.5) + episode."""

    name: ClassVar[str] = "enrich"

    def run(self, ctx: LoadContext) -> str:
        """Extract semantic axes and episode characters."""
        lateralized = self._laterality_groups(ctx)
        episodes = self._episodes(ctx)
        displaced = self._displacement_axis(ctx)
        return f"laterality on {lateralized:,}, episode on {episodes:,}, displacement on {displaced:,}"

    def _laterality_groups(self, ctx: LoadContext) -> int:
        groups: dict[tuple[str, str], list[dict]] = {}
        for concept in ctx.concepts.values():
            if concept["kind"] == "chapter":
                continue
            words = {m.group(1).lower() for m in _SIDE_RE.finditer(concept["display"])}
            if not words:
                continue
            # "Unspecified fracture of the RIGHT ulna": the sided word wins;
            # "unspecified" means side 9 only when no side word is present
            side = "1" if "right" in words else "2" if "left" in words else "9"
            stem = _SIDE_RE.sub("*", concept["display"].lower())
            stem = re.sub(r"\s+", " ", stem).strip()
            groups.setdefault((concept["code"][:3], stem), []).append({"concept": concept, "side": side})
        count = 0
        for (category, stem), members in groups.items():
            sides = {m["side"] for m in members}
            if not ({"1", "2"} & sides) or len(members) < 2:
                continue  # "unspecified" without sided siblings is not laterality
            group_key = f"{category}:{hashlib.sha1(stem.encode()).hexdigest()[:12]}"
            for member in members:
                member["concept"]["laterality"] = member["side"]
                member["concept"]["laterality_group"] = group_key
                member["concept"]["properties"].setdefault("axes", {})["laterality"] = {
                    "1": "right",
                    "2": "left",
                    "9": "unspecified",
                }[member["side"]]
                count += 1
        return count

    @staticmethod
    def _episodes(ctx: LoadContext) -> int:
        count = 0
        for concept in ctx.concepts.values():
            code = concept["code"].replace(".", "")
            if concept["kind"] != "code" or len(code) != 7:
                continue
            seventh = code[-1]
            if seventh not in EPISODE_CHARS:
                raise LoadError(f"{concept['code']}: unknown 7th character '{seventh}'")
            concept["episode"] = seventh
            concept["episode_group"] = concept["code"][:-1]
            count += 1
        return count

    @staticmethod
    def _displacement_axis(ctx: LoadContext) -> int:
        count = 0
        for concept in ctx.concepts.values():
            match = _DISPLACEMENT_RE.search(concept["display"])
            if match and concept["kind"] != "chapter":
                concept["properties"].setdefault("axes", {})["displacement"] = match.group(1).lower()
                count += 1
        return count


class LoadConceptsStage:
    """Stage 5: bulk-insert concepts (refuses a loaded FY without force)."""

    name: ClassVar[str] = "load"

    def run(self, ctx: LoadContext) -> str:
        """Bulk-insert concepts (idempotency guard via the ledger)."""
        concepts_t, edges_t, loads_t = _tables()
        existing = ctx.session.execute(
            select(func.count())
            .select_from(loads_t)
            .where(loads_t.c.ontology == ONTOLOGY, loads_t.c.fiscal_year == ctx.fiscal_year)
        ).scalar()
        if existing and not ctx.force:
            raise LoadError(f"FY{ctx.fiscal_year} is already loaded — re-run with --force to replace it")
        if ctx.force:
            ctx.session.execute(delete(edges_t))
            ctx.session.execute(delete(concepts_t).where(concepts_t.c.ontology == ONTOLOGY))
        # executemany needs homogeneous dicts — normalize against the table's
        # columns so optional fields (laterality, episode…) bind as NULL
        columns = [c.name for c in concepts_t.columns]
        rows = [{col: concept.get(col) for col in columns} for concept in ctx.concepts.values()]
        for i in range(0, len(rows), BATCH):
            ctx.session.execute(insert(concepts_t), rows[i : i + BATCH])
        ctx.session.flush()
        ctx.counters["concepts"] = len(rows)
        return f"{len(rows):,} concepts inserted"


class EdgesStage:
    """Stage 6 (order-file half): parent_of, contralateral, axis/episode variants."""

    name: ClassVar[str] = "edges"

    def run(self, ctx: LoadContext) -> str:
        """Build and insert the typed edge set."""
        self._parent_edges(ctx)
        self._contralateral_edges(ctx)
        self._episode_edges(ctx)
        self._displacement_edges(ctx)
        _concepts_t, edges_t, _loads_t = _tables()
        for i in range(0, len(ctx.edges), BATCH):
            ctx.session.execute(insert(edges_t), ctx.edges[i : i + BATCH])
        ctx.session.flush()
        ctx.counters["edges"] = len(ctx.edges)
        kinds: dict[str, int] = {}
        for edge in ctx.edges:
            kinds[edge["edge_type"]] = kinds.get(edge["edge_type"], 0) + 1
        return ", ".join(f"{n:,} {k}" for k, n in sorted(kinds.items()))

    def _add(self, ctx: LoadContext, source: str, target: str, edge_type: str, **props) -> None:
        ctx.edges.append(
            {
                "source_id": source,
                "target_id": target,
                "edge_type": edge_type,
                "authority": "DERIVED_LOADER",
                "confidence": 1.0,
                "properties": props or {},
            }
        )

    def _parent_edges(self, ctx: LoadContext) -> None:
        for concept in ctx.concepts.values():
            if concept["_parent"]:
                self._add(ctx, concept["_parent"], concept["id"], "parent_of")

    def _contralateral_edges(self, ctx: LoadContext) -> None:
        by_group: dict[str, dict[str, str]] = {}
        for concept in ctx.concepts.values():
            group, side = concept.get("laterality_group"), concept.get("laterality")
            if group and side in ("1", "2"):
                by_group.setdefault(group, {})[side] = concept["id"]
        for sides in by_group.values():
            if "1" in sides and "2" in sides:
                self._add(ctx, sides["1"], sides["2"], "contralateral")
                self._add(ctx, sides["2"], sides["1"], "contralateral")

    def _episode_edges(self, ctx: LoadContext) -> None:
        """Stem header → each 7th-character variant (linear, not pairwise)."""
        for concept in ctx.concepts.values():
            group = concept.get("episode_group")
            if not group:
                continue
            stem = ctx.concepts.get(f"{ONTOLOGY}:{group.rstrip('X')}")
            if stem:
                self._add(ctx, stem["id"], concept["id"], "episode_variant", episode=concept["episode"])

    def _displacement_edges(self, ctx: LoadContext) -> None:
        """displaced ↔ nondisplaced, same site and side (axis_variant)."""
        by_key: dict[str, dict[str, str]] = {}
        for concept in ctx.concepts.values():
            disp = concept.get("properties", {}).get("axes", {}).get("displacement")
            if not disp:
                continue
            stem = _DISPLACEMENT_RE.sub("*", concept["display"].lower())
            normalized = re.sub(r"\s+", " ", stem).strip()
            key = f"{concept['code'][:3]}:{normalized}"
            by_key.setdefault(key, {})[disp] = concept["id"]
        for pair in by_key.values():
            if "displaced" in pair and "nondisplaced" in pair:
                self._add(ctx, pair["displaced"], pair["nondisplaced"], "axis_variant", axis="displacement")
                self._add(ctx, pair["nondisplaced"], pair["displaced"], "axis_variant", axis="displacement")


class VerifyStage:
    """Stage 8: invariants over what was just written (design §4.2)."""

    name: ClassVar[str] = "verify"

    GOLDEN = ("E11.9", "S52.001A", "G89.4")

    def run(self, ctx: LoadContext) -> str:
        """Check post-load invariants; raise on any violation."""
        checks = [
            self._every_non_chapter_has_parent(ctx),
            self._billable_count_matches(ctx),
            self._laterality_groups_well_formed(ctx),
            self._golden_codes_present(ctx),
        ]
        return "; ".join(checks)

    @staticmethod
    def _every_non_chapter_has_parent(ctx: LoadContext) -> str:
        orphans = [c["code"] for c in ctx.concepts.values() if c["kind"] != "chapter" and not c["_parent"]]
        if orphans:
            raise LoadError(f"orphan concepts (no parent): {orphans[:5]}")
        return "no orphans"

    @staticmethod
    def _billable_count_matches(ctx: LoadContext) -> str:
        concepts_t, _e, _l = _tables()
        stored = ctx.session.execute(
            select(func.count()).select_from(concepts_t).where(concepts_t.c.is_billable)
        ).scalar()
        expected = sum(r.billable for r in ctx.rows)
        if stored != expected:
            raise LoadError(f"billable count mismatch: file {expected}, database {stored}")
        return f"{stored:,} billable verified"

    @staticmethod
    def _laterality_groups_well_formed(ctx: LoadContext) -> str:
        groups: dict[str, list[str]] = {}
        for c in ctx.concepts.values():
            if c.get("laterality_group"):
                groups.setdefault(c["laterality_group"], []).append(c["laterality"])
        for group, sides in groups.items():
            if len(sides) != len(set(sides)):
                raise LoadError(f"laterality group {group} has duplicate sides: {sides}")
        return f"{len(groups):,} laterality groups well-formed"

    def _golden_codes_present(self, ctx: LoadContext) -> str:
        missing = [g for g in self.GOLDEN if f"{ONTOLOGY}:{g}" not in ctx.concepts]
        if missing:
            raise LoadError(f"golden codes missing from load: {missing}")
        return "golden codes present"


class FinalizeStage:
    """Stage 9: write the ledger row — only reached if every stage passed."""

    name: ClassVar[str] = "finalize"

    def run(self, ctx: LoadContext) -> str:
        """Write the OntologyLoad ledger row and commit."""
        import time

        _c, _e, loads_t = _tables()
        duration = time.monotonic() - ctx.started
        ctx.session.execute(
            insert(loads_t),
            [
                {
                    "ontology": ONTOLOGY,
                    "fiscal_year": ctx.fiscal_year,
                    "source_checksums": ctx.checksums,
                    "concept_count": ctx.counters.get("concepts", 0),
                    "edge_count": ctx.counters.get("edges", 0),
                    "duration_seconds": round(duration, 2),
                    "properties": {"stages": "milestone-b-order-file"},
                }
            ],
        )
        ctx.session.commit()
        return f"ledger written ({duration:.1f}s)"
