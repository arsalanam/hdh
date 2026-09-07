"""CLI subcommand for the ontology module.  Registered by hdh.cli.

The ontology module owns cross-vocabulary `maps_to` edges. `hdh ontology tag`
backfills Condition.snomed_code and the three *derived* authorities from the
ICD-10 map; `hdh ontology crosswalk` loads an **official** licensed map (the
NLM ICD-10-CM ↔ SNOMED CT crosswalk, LOINC's equivalents) from a user-supplied
file through the same `LoadStage` pipeline the other loaders use — loader, not
data (issue #86).
"""

from __future__ import annotations


def register_cli(subparsers):
    """Register the `hdh ontology` subcommand."""
    p = subparsers.add_parser("ontology", help="SNOMED tagging + licensed crosswalk loaders")
    ontology_sub = p.add_subparsers(dest="ontology_cmd", required=True)
    ontology_sub.add_parser("tag", help="Backfill conditions.snomed_code from the ICD-10 map")

    cross_p = ontology_sub.add_parser(
        "crosswalk", help="Load an official licensed map (NLM ICD-10-CM↔SNOMED, LOINC↔SNOMED) from a file"
    )
    cross_p.add_argument("--source", required=True, help="The map file you obtained under your own licence")
    cross_p.add_argument(
        "--map",
        dest="map_name",
        default=None,
        help="Which map ('nlm-icd10cm-snomed', 'loinc-snomed'); inferred from the header if omitted",
    )
    cross_p.add_argument(
        "--release", type=int, help="Release tag YYYYMM (default: detected from the filename)"
    )
    cross_p.add_argument(
        "--force", action="store_true", help="Replace an already-loaded map of this authority"
    )

    ontology_sub.add_parser("crosswalk-status", help="Show loaded crosswalks and their maps_to edge counts")

    p.set_defaults(func=run)


def run(session, args):
    """Dispatch the parsed `hdh ontology` subcommand."""
    command = getattr(args, "ontology_cmd", "tag")
    if command == "crosswalk":
        _cmd_crosswalk(session, args)
    elif command == "crosswalk-status":
        _cmd_crosswalk_status(session)
    else:
        _cmd_tag(session)


def _cmd_crosswalk(session, args) -> None:
    """Run the crosswalk `LoadStage` pipeline over a user-supplied map file."""
    from hdh.modules.ontology.crosswalk import LoadError, run_load

    try:
        report = run_load(
            session, args.source, map_name=args.map_name, release=args.release, force=args.force
        )
    except LoadError as err:
        raise SystemExit(f"crosswalk load failed: {err}") from None
    for stage, summary in report:
        print(f"  {stage:<12} {summary}")


def _cmd_crosswalk_status(session) -> None:
    """Report loaded crosswalks from the ledger and live edge counts."""
    from sqlalchemy import func, select

    from hdh.core.models import Base
    from hdh.modules.ontology.crosswalk import SPECS

    tables = Base.metadata.tables
    loads_t, edges_t = tables["ontology_loads"], tables["ontology_edges"]
    tags = {spec.ledger_tag: spec for spec in SPECS.values()}
    rows = session.execute(select(loads_t).where(loads_t.c.ontology.in_(tags))).all()
    if not rows:
        print("No official crosswalk loaded. Run: hdh ontology crosswalk --source <map-file>")
        return
    for row in rows:
        spec = tags[row.ontology]
        live = session.execute(
            select(func.count())
            .select_from(edges_t)
            .where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == spec.authority)
        ).scalar()
        print(
            f"{spec.name}: release {row.fiscal_year}, {row.edge_count:,} edges at load, "
            f"{live:,} now live (authority {spec.authority})"
        )


def _cmd_tag(session) -> None:
    """Backfill SNOMED codes onto conditions from the derived mapping table
    (profile-authored > curated map > catalog-normalize; issue #29)."""
    from hdh.core.models import Condition

    from .derive import derive_mappings, record_maps_to_edges, tag_conditions

    if not hasattr(Condition, "snomed_code"):
        raise SystemExit(
            "Condition has no snomed_code column — the ontology schema module "
            "was not bootstrapped (this should not happen via the hdh CLI)."
        )

    from .symptoms import record_symptom_edges

    mappings = derive_mappings(session)
    counts = tag_conditions(session, mappings)
    edges = record_maps_to_edges(session, mappings)
    symptom_edges = record_symptom_edges(session)
    tagged = sum(counts.values())
    untagged = session.query(Condition).filter(Condition.snomed_code.is_(None)).count()
    print(
        f"🏷  SNOMED-tagged {tagged:,} conditions "
        f"({counts['profile']:,} profile-authored, {counts['curated']:,} curated, "
        f"{counts['derived']:,} derived from the loaded catalogs) · "
        f"{edges:,} maps_to edges recorded · {untagged:,} remain unmapped"
    )
    print(
        f"🩺 {symptom_edges:,} curated symptom maps_to edges — free-text complaints "
        "(headache, fatigue, dizziness…) now carry a billing view"
    )
