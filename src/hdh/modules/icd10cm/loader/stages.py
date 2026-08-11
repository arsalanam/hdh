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
_SIDE_RE = re.compile(r"\b(right|left|bilateral|unspecified)\b", re.IGNORECASE)
_DISPLACEMENT_RE = re.compile(r"\b(nondisplaced|displaced)\b", re.IGNORECASE)
_ENCOUNTER_WORDS = ("encounter", "sequela")


def _longest_prefix_match(dotted: str, defs: dict) -> str | None:
    """The most specific sevenChrDef family covering a code, if any."""
    for length in range(len(dotted), 2, -1):
        candidate = dotted[:length].rstrip(".")
        if candidate in defs:
            return candidate
    return None


def _one_char_subgroups(members: list) -> list:
    """Split same-stem candidates into true variant sets.

    Identical descriptions can belong to unrelated codes (H54.2X1 and
    H54.511 are both "Low vision, right eye, category 1"). True laterality
    variants differ in exactly ONE code character (the side position), so
    subgroup membership requires hamming distance 1 to the subgroup seed.
    """
    subgroups: list[list] = []
    for member in members:
        code = member["concept"]["code"]
        for subgroup in subgroups:
            seed = subgroup[0]["concept"]["code"]
            if len(seed) == len(code) and sum(a != b for a, b in zip(seed, code, strict=True)) == 1:
                subgroup.append(member)
                break
        else:
            subgroups.append([member])
    return subgroups


def _is_encounter_definition(meaning: str) -> bool:
    """True when a 7th-char definition describes an episode of care."""
    lowered = meaning.lower()
    return any(word in lowered for word in _ENCOUNTER_WORDS)


def _tables():
    """The registry-materialized tables (resolved lazily, after bootstrap)."""
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_loads"]


class AcquireStage:
    """Stage 1: locate the source files and record their checksums."""

    name: ClassVar[str] = "acquire"

    def run(self, ctx: LoadContext) -> str:
        """Locate the release files (order required, tabular optional)."""
        order_file = self._find(ctx, "order", ".txt")
        if order_file is None:
            raise LoadError(
                f"no order file for FY{ctx.fiscal_year} in {ctx.source_dir} — "
                "use --download or place the CMS release files there"
            )
        ctx.files["order"] = order_file
        ctx.checksums[order_file.name] = hashlib.sha256(order_file.read_bytes()).hexdigest()
        summary = f"{order_file.name} ({order_file.stat().st_size:,} bytes)"
        tabular_file = self._find(ctx, "tabular", ".xml")
        if tabular_file is not None:
            ctx.files["tabular"] = tabular_file
            ctx.checksums[tabular_file.name] = hashlib.sha256(tabular_file.read_bytes()).hexdigest()
            summary += f" + {tabular_file.name}"
        return summary

    @staticmethod
    def _find(ctx: LoadContext, kind: str, suffix: str):
        """Match both normalized (dash) and CMS-native (underscore) names."""
        for pattern in (
            f"icd10cm-{kind}-{ctx.fiscal_year}*{suffix}",
            f"icd10cm_{kind}_{ctx.fiscal_year}*{suffix}",
        ):
            matches = sorted(ctx.source_dir.glob(pattern))
            if matches:
                return matches[0]
        return None


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


class TabularStage:
    """Stage 3 (XML half): blocks, coding-rule notes, 7th-char definitions.

    Skipped gracefully when only the order file is present — the hierarchy
    then stays chapter → category and no rule edges are built.
    """

    name: ClassVar[str] = "tabular"

    def run(self, ctx: LoadContext) -> str:
        """Add the block level, attach rule notes, collect 7th-char defs."""
        if "tabular" not in ctx.files:
            return "no tabular XML — blocks and coding rules skipped"
        from hdh.modules.icd10cm.loader.tabular import parse_tabular

        ctx.tabular = parse_tabular(ctx.files["tabular"])
        blocks = self._add_blocks(ctx)
        reparented = self._reparent_categories(ctx)
        self._recompute_paths(ctx)
        noted = self._attach_notes(ctx)
        return (
            f"{blocks} blocks, {reparented} categories re-parented, "
            f"rule notes on {noted}, 7th-char defs for {len(ctx.tabular.seven_defs)} families"
        )

    def _add_blocks(self, ctx: LoadContext) -> int:
        used_categories = {c["code"][:3] for c in ctx.concepts.values() if c["kind"] != "chapter"}
        added = 0
        for block in ctx.tabular.blocks:
            if not any(block.contains(cat) for cat in used_categories):
                continue
            if f"{ONTOLOGY}:{block.range_code}" in ctx.concepts:
                # single-code sections (B20, F99, R99…) ARE their category —
                # no separate block node, hierarchy stays chapter → category
                continue
            chapter = chapter_for(block.first)
            if chapter is None:
                raise LoadError(f"block {block.range_code}: no chapter covers {block.first}")
            ctx.concepts[f"{ONTOLOGY}:{block.range_code}"] = {
                "id": f"{ONTOLOGY}:{block.range_code}",
                "ontology": ONTOLOGY,
                "code": block.range_code,
                "kind": "block",
                "display": block.description,
                "short_display": block.description[:128],
                "is_billable": False,
                "hierarchy_depth": 1,
                "path": f"{chapter.path_segment}.{block.range_code}",
                "properties": {},
                "effective_fy": ctx.fiscal_year,
                "_parent": chapter.concept_id,
            }
            added += 1
        return added

    def _reparent_categories(self, ctx: LoadContext) -> int:
        count = 0
        for concept in ctx.concepts.values():
            if len(concept["code"]) != 3 or concept["kind"] in ("chapter", "block"):
                continue
            block = ctx.tabular.block_for(concept["code"])
            if (
                block
                and block.range_code != concept["code"]  # never self-parent (B20 case)
                and f"{ONTOLOGY}:{block.range_code}" in ctx.concepts
            ):
                concept["_parent"] = f"{ONTOLOGY}:{block.range_code}"
                count += 1
        return count

    @staticmethod
    def _recompute_paths(ctx: LoadContext) -> None:
        """Re-derive every path/depth from the (possibly new) parent chain."""
        rank = {"chapter": 0, "block": 1}
        ordered = sorted(
            ctx.concepts.values(),
            key=lambda c: (rank.get(c["kind"], 2), len(c["code"].replace(".", ""))),
        )
        for concept in ordered:
            if concept["kind"] == "chapter":
                continue
            parent = ctx.concepts[concept["_parent"]]
            concept["path"] = f"{parent['path']}.{concept['code'].replace('.', '')}"
            concept["hierarchy_depth"] = parent["hierarchy_depth"] + 1

    @staticmethod
    def _attach_notes(ctx: LoadContext) -> int:
        noted = 0
        for dotted, notes in ctx.tabular.rules.items():
            concept = ctx.concepts.get(f"{ONTOLOGY}:{dotted}")
            if concept is None:
                continue
            grouped = concept["properties"].setdefault("notes", {})
            for note in notes:
                grouped.setdefault(note.edge_type, []).append(note.text)
            noted += 1
        return noted


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
            if "right" in words and "left" in words:
                continue  # composite descriptor (H54 "blindness right eye,
                #           normal vision left eye") — laterality undefined
            # "Unspecified fracture of the RIGHT ulna": the sided word wins;
            # "unspecified" means side 9 only when no side word is present
            side = (
                "1" if "right" in words else "2" if "left" in words else "3" if "bilateral" in words else "9"
            )
            stem = _SIDE_RE.sub("*", concept["display"].lower())
            stem = re.sub(r"\s+", " ", stem).strip()
            groups.setdefault((concept["code"][:3], stem), []).append({"concept": concept, "side": side})
        count = 0
        for (category, stem), members in groups.items():
            for index, subgroup in enumerate(_one_char_subgroups(members)):
                sides = {m["side"] for m in subgroup}
                if not ({"1", "2"} & sides) or len(subgroup) < 2:
                    continue  # "unspecified" without sided siblings is not laterality
                digest = hashlib.sha1(stem.encode()).hexdigest()[:12]
                group_key = f"{category}:{digest}:{index}"
                for member in subgroup:
                    member["concept"]["laterality"] = member["side"]
                    member["concept"]["laterality_group"] = group_key
                    member["concept"]["properties"].setdefault("axes", {})["laterality"] = {
                        "1": "right",
                        "2": "left",
                        "3": "bilateral",
                        "9": "unspecified",
                    }[member["side"]]
                    count += 1
        return count

    @staticmethod
    def _episodes(ctx: LoadContext) -> int:
        """Assign episode from the 7th character — but only where it means
        one: the XML's sevenChrDef is authoritative (obstetric codes carry
        fetus digits there, not encounters); without the XML the
        conservative letter set applies and unknown characters are left
        unclassified rather than guessed."""
        seven_defs = ctx.tabular.seven_defs if ctx.tabular else {}
        count = 0
        for concept in ctx.concepts.values():
            code = concept["code"].replace(".", "")
            if concept["kind"] != "code" or len(code) != 7:
                continue
            seventh = code[-1]
            family = _longest_prefix_match(concept["code"], seven_defs)
            if family is not None:
                definitions = seven_defs[family]
                if seventh not in definitions:
                    raise LoadError(
                        f"{concept['code']}: 7th character '{seventh}' not in "
                        f"{family} sevenChrDef {sorted(definitions)}"
                    )
                if not _is_encounter_definition(definitions[seventh]):
                    continue  # a fetus digit or similar — not an episode
            elif seventh not in EPISODE_CHARS:
                continue  # no authority to classify — leave unset
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
        self._rule_edges(ctx)
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
        authority = props.pop("authority", "DERIVED_LOADER")
        ctx.edges.append(
            {
                "source_id": source,
                "target_id": target,
                "edge_type": edge_type,
                "authority": authority,
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

    def _rule_edges(self, ctx: LoadContext) -> None:
        """Coding-rule edges from tabular notes with resolvable code refs.

        A note referencing a code outside the loaded catalog produces no
        edge — the note text itself is already on the source concept's
        properties (TabularStage), so nothing is lost."""
        if ctx.tabular is None:
            return
        for dotted, notes in ctx.tabular.rules.items():
            source = ctx.concepts.get(f"{ONTOLOGY}:{dotted}")
            if source is None:
                continue
            for note in notes:
                for ref in note.refs:
                    target = ctx.concepts.get(f"{ONTOLOGY}:{ref}")
                    if target is not None and target["id"] != source["id"]:
                        self._add(
                            ctx,
                            source["id"],
                            target["id"],
                            note.edge_type,
                            authority="CMS_TABULAR",
                            note=note.text,
                        )


class AccelerateStage:
    """Stage 7: PostgreSQL accelerators (design §3.4) — expression-GIN FTS
    and trigram indexes. A no-op on other dialects; all statements are
    static and idempotent (IF NOT EXISTS)."""

    name: ClassVar[str] = "accelerate"

    DDL = (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS ix_concept_fts ON ontology_concepts "
        "USING GIN (to_tsvector('english', code || ' ' || display))",
        "CREATE INDEX IF NOT EXISTS ix_concept_trgm ON ontology_concepts USING GIN (display gin_trgm_ops)",
        # plain btree can't serve LIKE prefix scans on PG — the reference
        # architecture's own text_pattern_ops lesson (design §2 departure 4)
        "CREATE INDEX IF NOT EXISTS ix_concept_path_prefix ON ontology_concepts (path text_pattern_ops)",
    )

    def run(self, ctx: LoadContext) -> str:
        """Create the PostgreSQL search indexes (skips other dialects)."""
        from sqlalchemy import text

        bind = ctx.session.get_bind()
        if bind.dialect.name != "postgresql":
            return f"skipped ({bind.dialect.name})"
        for statement in self.DDL:
            ctx.session.execute(text(statement))
        ctx.session.flush()
        return f"{len(self.DDL) - 1} search indexes ensured (+pg_trgm)"


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
