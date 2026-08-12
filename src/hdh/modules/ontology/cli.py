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
    """Backfill SNOMED codes onto conditions using the registry-added columns."""
    from hdh.core.models import Condition

    from . import ICD10_TO_SNOMED

    if not hasattr(Condition, "snomed_code"):
        raise SystemExit(
            "Condition has no snomed_code column — the ontology schema module "
            "was not bootstrapped (this should not happen via the hdh CLI)."
        )

    tagged = 0
    for icd10, (snomed_id, display) in ICD10_TO_SNOMED.items():
        tagged += (
            session.query(Condition)
            .filter(Condition.icd10_code == icd10, Condition.snomed_code.is_(None))
            .update({"snomed_code": snomed_id, "snomed_display": display})
        )
    session.commit()
    untagged = session.query(Condition).filter(Condition.snomed_code.is_(None)).count()
    print(f"🏷  SNOMED-tagged {tagged:,} conditions ({untagged:,} remain unmapped — extend ICD10_TO_SNOMED)")
