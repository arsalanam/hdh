"""Schema registry: modules extend core entity definitions declaratively.

This implements the extensible-schema design from
``docs/design/original-design-notes.md`` (§7–§13) in hybrid form: the static
classes in ``models.py`` are the implicit **base module**, and extension
modules ship JSON schema files that *append* columns (and relationships /
whole new entities) at bootstrap time — before any engine or session exists.

A schema module is a directory containing::

    manifest.json                  {name, version, depends_on, priority}
    schema/entities/*.json         columns + indexes only   (phases 1–2)
    schema/relationships/*.json    relationships only       (phases 3–4)

Load order (the four phases from the design, then the two factory passes)::

    Phase 1  load every module's entity specs
    Phase 2  merge per entity — later module wins on new-column collisions
             (logged warning); re-declaring a BASE column or renaming a
             tablename is a hard error
    Phase 3  load relationship specs; unknown targets are a hard error
    Phase 4  merge relationships — later module wins (logged warning)
    Pass 1   apply columns: inject into existing mapped classes, build new
             entity classes on Base
    Pass 2   wire relationships (both sides now guaranteed to exist)

``bootstrap_schema()`` runs the whole sequence once per process and must be
called before ``get_engine()`` touches the database; ``get_engine`` then
auto-adds any missing (nullable) columns to an existing SQLite file — the
lightweight stand-in for the Alembic step in the original design.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from importlib import import_module
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

log = logging.getLogger("hdh.schema")

BASE_MODULE = "base"  # reserved name for the static models.py classes


class SchemaError(Exception):
    """A hard schema violation (collision rules from the design doc)."""


def _build_column(spec: dict, entity: str) -> Column:
    """Translate one JSON column spec into a SQLAlchemy Column."""
    type_name = spec["type"]
    col_type: Any
    if type_name == "String":
        col_type = String(spec.get("length", 80))
    elif type_name == "Enum":
        col_type = SAEnum(*spec["values"], name=f"{entity.lower()}_{spec['name']}_enum")
    elif type_name == "JSON":
        # Portable JSON that upgrades to JSONB (GIN-indexable) on PostgreSQL
        col_type = JSON().with_variant(JSONB(), "postgresql")
    else:
        simple = {
            "Integer": Integer,
            "Float": Float,
            "Date": Date,
            "DateTime": DateTime,
            "Boolean": Boolean,
            "Text": Text,
        }
        try:
            col_type = simple[type_name]()
        except KeyError:
            raise SchemaError(f"{entity}: unsupported column type '{type_name}'") from None
    args = [ForeignKey(spec["foreign_key"])] if spec.get("foreign_key") else []
    kwargs: dict[str, Any] = {}
    if spec.get("server_default") is not None:
        kwargs["server_default"] = text(str(spec["server_default"]))
    return Column(
        spec["name"],
        col_type,
        *args,
        primary_key=spec.get("primary_key", False),
        nullable=spec.get("nullable", True),
        autoincrement=spec.get("autoincrement", "auto"),
        **kwargs,
    )


def _build_indexes(merged: dict, table) -> list[Index]:
    """Materialize an entity spec's "indexes" list against a live table.

    Supported spec keys: name, columns (list), unique (bool), where (SQL
    text for partial indexes — dialect support applies at create time).
    """
    built = []
    existing = {ix.name for ix in table.indexes}
    for ix_spec in merged.get("indexes", []):
        if ix_spec["name"] in existing:
            continue  # idempotent re-apply
        try:
            cols = [table.c[name] for name in ix_spec["columns"]]
        except KeyError as err:
            raise SchemaError(f"index {ix_spec['name']}: unknown column {err} on {table.name}") from None
        kwargs: dict[str, Any] = {"unique": ix_spec.get("unique", False)}
        if ix_spec.get("where"):
            kwargs["postgresql_where"] = text(ix_spec["where"])
        built.append(Index(ix_spec["name"], *cols, **kwargs))
    return built


def _base_entities() -> dict[str, type]:
    """The static models.py classes — the implicit base module."""
    from hdh.core.models import Base

    return {mapper.class_.__name__: mapper.class_ for mapper in Base.registry.mappers}


@dataclass
class _Module:
    """One registered schema module (manifest + spec directory)."""

    name: str
    path: Path
    depends_on: tuple[str, ...] = ()
    priority: int = 10
    version: str = "0"


@dataclass
class SchemaRegistry:
    """Loads, merges, and applies module schema extensions (see module docs)."""

    modules: list[_Module] = field(default_factory=list)
    merged_entities: dict = field(default_factory=dict)  # entity -> merged spec
    merged_relationships: dict = field(default_factory=dict)  # entity -> {name: (spec, module)}
    applied: bool = False
    new_classes: dict = field(default_factory=dict)  # entities created by the factory

    # ── Registration ─────────────────────────────────────────────────────────

    def register_module(self, package_or_path: str) -> None:
        """Register a schema module by dotted package name or directory path."""
        path = Path(package_or_path)
        if not path.is_dir():
            pkg = import_module(package_or_path)
            path = Path(pkg.__file__).parent  # type: ignore[arg-type]
        manifest_file = path / "manifest.json"
        if not manifest_file.exists():
            manifest_file = path / "schema" / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.modules.append(
            _Module(
                name=manifest["name"],
                path=path,
                depends_on=tuple(manifest.get("depends_on", [])),
                priority=int(manifest.get("priority", 10)),
                version=str(manifest.get("version", "0")),
            )
        )

    def _ordered_modules(self) -> list[_Module]:
        """Dependency order (topological), then priority, then registration."""
        by_name = {m.name: m for m in self.modules}
        graph = {m.name: set(d for d in m.depends_on if d != BASE_MODULE) for m in self.modules}
        try:
            topo = list(TopologicalSorter(graph).static_order())
        except CycleError as err:
            raise SchemaError(f"circular module dependency: {err.args[1]}") from None
        rank = {name: i for i, name in enumerate(topo)}
        return sorted(by_name.values(), key=lambda m: (rank[m.name], m.priority))

    # ── Phases 1–4 ───────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Run the four load phases; hard errors follow the design's rules."""
        base = _base_entities()
        ordered = self._ordered_modules()
        self._load_and_merge_entities(ordered, base)  # phases 1 + 2
        self._load_and_merge_relationships(ordered, base)  # phases 3 + 4

    def _load_and_merge_entities(self, ordered: list[_Module], base: dict) -> None:
        """Phases 1–2: read entity specs and merge with collision rules."""
        for module in ordered:
            for spec_file in sorted((module.path / "schema" / "entities").glob("*.json")):
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
                entity = spec["entity"]
                is_base_entity = entity in base
                if is_base_entity and spec.get("tablename") not in (
                    None,
                    base[entity].__tablename__,
                ):
                    raise SchemaError(
                        f"{module.name}/{spec_file.name}: extension may not rename tablename of {entity}"
                    )
                merged = self.merged_entities.setdefault(
                    entity,
                    {
                        "tablename": spec.get("tablename"),
                        "columns": {},
                        "indexes": [],
                        "is_new": not is_base_entity,
                        "fhir": None,
                    },
                )
                if merged["is_new"] and spec.get("tablename"):
                    merged["tablename"] = spec["tablename"]
                merged["indexes"].extend(spec.get("indexes", []))
                self._merge_fhir_hint(spec, merged, module, spec_file, is_base_entity)
                base_columns = {c.name for c in base[entity].__table__.columns} if is_base_entity else set()
                for column_spec in spec.get("columns", []):
                    name = column_spec["name"]
                    if name in base_columns:
                        raise SchemaError(
                            f"{module.name}/{spec_file.name}: column '{name}' re-declares a "
                            f"base column of {entity} (extensions add only NEW columns)"
                        )
                    if name in merged["columns"]:
                        prev_module = merged["columns"][name][1]
                        log.warning(
                            "schema merge: %s.%s from %s overrides %s (later module wins)",
                            entity,
                            name,
                            module.name,
                            prev_module,
                        )
                    merged["columns"][name] = (column_spec, module.name)
        for entity, merged in self.merged_entities.items():
            if merged["is_new"] and not merged["tablename"]:
                raise SchemaError(f"new entity {entity} defines no tablename")
            if merged["fhir"]:
                self._validate_fhir_hint(entity, merged)

    @staticmethod
    def _merge_fhir_hint(spec, merged, module, spec_file, is_base_entity: bool) -> None:
        """Capture an entity spec's optional ``fhir`` export hint (design
        fhir-emitters.md §7). Hints are for NEW entities only — base chart
        resources have hand-written typed emitters in hdh.core.fhir."""
        hint = spec.get("fhir")
        if hint is None:
            return
        if is_base_entity:
            raise SchemaError(
                f"{module.name}/{spec_file.name}: fhir hints are for new (module-declared) "
                f"entities; {spec['entity']} is a base entity with a hand-written emitter"
            )
        if merged["fhir"] is not None:
            log.warning(
                "schema merge: fhir hint for %s from %s overrides %s (later module wins)",
                spec["entity"],
                module.name,
                merged["fhir"][1],
            )
        merged["fhir"] = (hint, module.name)

    @staticmethod
    def _validate_fhir_hint(entity: str, merged: dict) -> None:
        """Fail at bootstrap, not export: hints must be complete and only
        reference declared columns."""
        hint, module = merged["fhir"]
        for key in ("resourceType", "patient_link"):
            if not hint.get(key):
                raise SchemaError(f"{module}: fhir hint for {entity} is missing '{key}'")
        declared = set(merged["columns"])
        unknown = (
            {hint["patient_link"], *hint.get("fields", {})} | set(hint.get("id_fields", []))
        ) - declared
        if unknown:
            raise SchemaError(
                f"{module}: fhir hint for {entity} references unknown column(s): {', '.join(sorted(unknown))}"
            )

    def _load_and_merge_relationships(self, ordered: list[_Module], base: dict) -> None:
        """Phases 3–4: read relationship specs, validate targets, merge."""
        known = set(base) | {e for e, m in self.merged_entities.items() if m["is_new"]}
        for module in ordered:
            rel_dir = module.path / "schema" / "relationships"
            for spec_file in sorted(rel_dir.glob("*.json")) if rel_dir.exists() else []:
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
                entity = spec["entity"]
                if entity not in known:
                    raise SchemaError(
                        f"{module.name}/{spec_file.name}: relationships declared for unknown entity {entity}"
                    )
                merged = self.merged_relationships.setdefault(entity, {})
                for rel in spec.get("relationships", []):
                    if rel["target"] not in known:
                        raise SchemaError(
                            f"{module.name}/{spec_file.name}: {entity}.{rel['name']} targets "
                            f"unknown entity {rel['target']}"
                        )
                    if rel["name"] in merged:
                        log.warning(
                            "schema merge: %s.%s from %s overrides %s (later module wins)",
                            entity,
                            rel["name"],
                            module.name,
                            merged[rel["name"]][1],
                        )
                    merged[rel["name"]] = (rel, module.name)

    # ── Factory passes 1–2 ───────────────────────────────────────────────────

    def apply(self, entities: dict[str, type] | None = None) -> dict[str, type]:
        """Two-pass factory: inject columns / build new classes, then wire
        relationships. Returns the full entity map (base + new)."""
        from hdh.core.models import Base

        classes: dict[str, Any] = dict(entities if entities is not None else _base_entities())
        for entity, merged in self.merged_entities.items():  # pass 1
            if merged["is_new"]:
                attrs: dict = {"__tablename__": merged["tablename"]}
                attrs.update({n: _build_column(spec, entity) for n, (spec, _m) in merged["columns"].items()})
                attrs["__doc__"] = f"Entity defined by schema module(s) via the registry: {entity}."
                classes[entity] = type(entity, (Base,), attrs)
                self.new_classes[entity] = classes[entity]
            else:
                target = classes[entity]
                for name, (spec, _module) in merged["columns"].items():
                    if name in target.__table__.columns:
                        # idempotent re-apply; spec-vs-database drift is
                        # Alembic autogenerate's job (issue #7)
                        continue
                    column = _build_column(spec, entity)
                    target.__table__.append_column(column)
                    target.__mapper__.add_property(name, column)
            # Index() attaches itself to the table's metadata at construction
            _build_indexes(merged, classes[entity].__table__)
        for entity, rels in self.merged_relationships.items():  # pass 2
            target = classes[entity]
            for name, (spec, _module) in rels.items():
                if spec["type"] == "many_to_many":
                    raise SchemaError(
                        f"{entity}.{name}: many_to_many is not supported (no association-"
                        "table specs yet) — model an explicit link entity instead"
                    )
                kwargs: dict = {}
                if spec.get("back_populates"):
                    kwargs["back_populates"] = spec["back_populates"]
                if spec.get("cascade"):
                    kwargs["cascade"] = spec["cascade"]
                if spec.get("foreign_keys"):
                    kwargs["foreign_keys"] = self._resolve_columns(entity, name, spec["foreign_keys"])
                uselist = spec["type"] == "one_to_many"
                target.__mapper__.add_property(
                    name, relationship(classes[spec["target"]], uselist=uselist, **kwargs)
                )
        self.applied = True
        return classes

    @staticmethod
    def _resolve_columns(entity: str, rel: str, refs: list[str]) -> list[Column]:
        """Resolve "table.column" strings from a relationship's foreign_keys
        spec — required when two FKs point at the same target table (e.g. a
        graph edge's source/target both referencing a concepts table)."""
        from hdh.core.models import Base

        columns = []
        for ref in refs:
            table_name, _, col_name = ref.partition(".")
            table = Base.metadata.tables.get(table_name)
            if table is None or col_name not in table.c:
                raise SchemaError(f"{entity}.{rel}: foreign_keys references unknown column '{ref}'")
            columns.append(table.c[col_name])
        return columns

    # ── Runtime helpers ──────────────────────────────────────────────────────

    def ensure_columns(self, engine) -> list[str]:
        """Add extension columns missing from an existing database (ALTER
        TABLE ADD COLUMN) — the lightweight stand-in for databases not yet
        under Alembic. Once a database carries an ``alembic_version`` table,
        Alembic owns its schema and this auto-ADD path steps aside
        (issue #7; run `just db-upgrade` instead)."""
        added: list[str] = []
        inspector = inspect(engine)
        if inspector.has_table("alembic_version"):
            return added
        from hdh.core.models import Base

        for table in Base.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
                )
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
        if added:
            log.info("schema: added missing columns: %s", ", ".join(added))
        return added

    def describe(self) -> str:
        """Human-readable summary: module order, extensions, new entities."""
        lines = ["Schema registry"]
        ordered = self._ordered_modules()
        chain = " → ".join([BASE_MODULE] + [m.name for m in ordered])
        lines.append(f"  module load order: {chain}")
        if not self.merged_entities:
            lines.append("  no extensions registered")
        for entity, merged in sorted(self.merged_entities.items()):
            kind = "NEW entity" if merged["is_new"] else "extends base"
            cols = ", ".join(f"{n} ({m})" for n, (_s, m) in merged["columns"].items())
            fhir = f" → FHIR {merged['fhir'][0]['resourceType']}" if merged.get("fhir") else ""
            lines.append(f"  {entity} [{kind}]: {cols}{fhir}")
        for entity, rels in sorted(self.merged_relationships.items()):
            names = ", ".join(rels)
            lines.append(f"  {entity} relationships: {names}")
        return "\n".join(lines)


# ── Process-wide bootstrap (the app.py sequence from the design) ─────────────

registry = SchemaRegistry()


def bootstrap_schema(module_names: list[str] | None = None) -> SchemaRegistry:
    """Run register → load_all → apply once per process.

    Must run before get_engine() so extension columns exist in metadata when
    tables are created (or auto-added to an existing database). Idempotent:
    later calls return the already-applied registry.
    """
    if registry.applied:
        return registry
    if module_names is None:
        from hdh.modules import SCHEMA_MODULES

        module_names = list(SCHEMA_MODULES)
    for name in module_names:
        registry.register_module(name)
    registry.load_all()
    registry.apply()
    return registry
