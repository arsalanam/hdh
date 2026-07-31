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
    "hdh.modules.narrative.cli": None,
    "hdh.modules.fhir_api.cli": "api",
}
