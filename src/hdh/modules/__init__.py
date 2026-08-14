"""Feature modules built on top of ``hdh.core``.

Each module is optional and self-contained. Modules may depend on the core
and on optional third-party extras, but never on each other's internals.
A module can expose CLI subcommands by providing a ``register_cli(subparsers)``
function; ``hdh.cli`` discovers these lazily.
"""

# Modules with CLI subcommands, in display order. Each entry maps the module's
# import path to the pip extra that provides its dependencies (None = core only).
CLI_MODULES = {
    "hdh.modules.caregaps.cli": None,
    "hdh.modules.risk.cli": "risk",
    "hdh.modules.agent.cli": "agent",
    "hdh.modules.agent.pipeline.trace_cli": "agent",
    "hdh.modules.ontology.cli": None,
    "hdh.modules.icd10cm.cli": None,
    "hdh.modules.snomed.cli": None,
    "hdh.modules.narrative.cli": None,
    "hdh.modules.fhir_api.cli": "api",
}

# Modules contributing FHIR enrichers/emitters (each exposes fhir_enrichers();
# see hdh.core.fhir — design docs/design/fhir-emitters.md)
FHIR_MODULES = ("hdh.modules.ontology.fhir",)

# Vocabulary modules implementing the OntologyService protocol (each exposes
# build_service(session); see hdh.core.ontology — consumers dispatch through
# get_ontology_service and never query hierarchy storage directly).
ONTOLOGY_MODULES = {
    "icd10cm": "hdh.modules.icd10cm.ontology",
    "snomed_ct": "hdh.modules.snomed.ontology",
}

# Modules that extend the data model via the schema registry (each ships a
# manifest.json + schema/ directory; see hdh.core.schema_registry).
SCHEMA_MODULES = (
    "hdh.modules.ontology",
    "hdh.modules.icd10cm",
    "hdh.modules.snomed",
)
