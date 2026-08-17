"""CLI subcommand for the ontology module.  Registered by hdh.cli.

The ontology module extends the Condition entity with snomed_code /
snomed_display via the schema registry (see schema/entities/diagnosis.json);
`hdh ontology tag` backfills those columns from the ICD-10 map.
"""


def register_cli(subparsers):
    """Register the `hdh ontology` subcommand."""
    p = subparsers.add_parser("ontology", help="SNOMED tagging (schema-registry extension demo)")
    ontology_sub = p.add_subparsers(dest="ontology_cmd", required=True)
    ontology_sub.add_parser("tag", help="Backfill conditions.snomed_code from the ICD-10 map")
    p.set_defaults(func=run)


def run(session, args):
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
