"""The agent's LOINC toolset.

LOINC was the only ontology module without one (#77), which mattered more
after §10.0 of the comprehension design made it the vocabulary for
**ordering a lab** — the single lab-shaped thing a note is allowed to
produce. An agent that charts a note could not check or correct the code
the order would carry.

These follow design §7 like every other toolset: **an agent tool may not
contain a decision a non-agent caller would also need.** The specimen
preference, the default-to-blood rule and the ranking all live in
``LoincOntologyService`` and are reached identically by ``hdh loinc``. A
tool with its own copy would let the agent and the CLI disagree about
which sodium was meant, and only one of them is tested.
"""

from __future__ import annotations

import json

from sqlalchemy import select

#: The axes worth showing an agent. LOINC has six; `time`, `scale` and
#: `method` distinguish codes that a note almost never distinguishes, so
#: they would be noise in a tool result that has to be read in context.
_SHOWN_AXES = ("component", "property", "system")


def build_loinc_tools(session) -> list:
    """The agent's LOINC tools bound to an open session (empty if no catalog)."""
    from anthropic import beta_tool

    from hdh.core.models import Base, tool_guard
    from hdh.modules.loinc.ontology import build_service

    guard = tool_guard(session)
    concepts_t = Base.metadata.tables["ontology_concepts"]
    if (
        session.execute(select(concepts_t.c.id).where(concepts_t.c.ontology == "loinc").limit(1)).first()
        is None
    ):
        return []  # catalog not loaded — don't offer tools that can only fail
    service = build_service(session)

    def _describe(concept) -> dict:
        properties = concept.properties or {}
        return {
            "loinc": concept.code,
            "display": concept.display,
            **{axis: properties.get(axis) for axis in _SHOWN_AXES if properties.get(axis)},
            "class": properties.get("class"),
        }

    @beta_tool
    @guard
    def loinc_search(mention: str, specimen: str = "", classes: str = "", limit: int = 5) -> str:
        """Map a lab or vital sign to ranked LOINC codes. Pass the test as written in the note. Say the specimen when the note says it — a bare "sodium" matches serum, urine, CSF and dialysate equally on text and they are different tests; unqualified means blood, which is what an unqualified order means in family medicine.

        Args:
            mention: The test as written, e.g. "HbA1c" or "urine sodium".
            specimen: The specimen if stated, e.g. "urine", "ser/plas", "csf".
            classes: Comma-separated LOINC CLASS values to prefer, e.g. "CHEM,HEM/BC".
            limit: Maximum candidates to return.
        """
        context: dict = {"limit": limit}
        if specimen:
            context["system"] = specimen
        if classes:
            context["classes"] = [c.strip() for c in classes.split(",") if c.strip()]
        candidates = service.normalize(mention, context)
        if not candidates:
            return "No matches in the loaded LOINC catalog."
        return json.dumps(
            [{**_describe(c.concept), "score": c.score, "why": c.reason} for c in candidates],
            indent=1,
        )

    @beta_tool
    @guard
    def loinc_lookup(code: str) -> str:
        """Everything the catalog holds about one LOINC code: its display, its six defining axes, and its synonyms. Use it to confirm a code says what you think before an order carries it.

        Args:
            code: The LOINC code, e.g. "4548-4".
        """
        concept = service.lookup(code)
        if concept is None:
            return f"No LOINC concept {code} in the loaded catalog."
        properties = concept.properties or {}
        return json.dumps(
            {
                **_describe(concept),
                "axes": {
                    axis: properties.get(axis)
                    for axis in ("component", "property", "time", "system", "scale", "method")
                },
                "status": properties.get("status"),
                "synonyms": list(service.synonyms(code)),
            },
            indent=1,
        )

    @beta_tool
    @guard
    def loinc_specimen_variants(code: str, limit: int = 10) -> str:
        """The same measurement on other specimens — the serum/urine/CSF question, answered from the catalog rather than guessed. Use this when a note names a test without saying what was sampled, or to check an order is asking for the specimen the plan intended.

        Args:
            code: A LOINC code whose component to match, e.g. "2951-2" (serum sodium).
            limit: Maximum variants to return.
        """
        concept = service.lookup(code)
        if concept is None:
            return f"No LOINC concept {code} in the loaded catalog."
        component = (concept.properties or {}).get("component")
        if not component:
            return f"{code} records no component axis — nothing to match on."
        rows = session.execute(
            select(concepts_t)
            .where(
                concepts_t.c.ontology == "loinc",
                # .as_string() and not .astext: the latter is PostgreSQL-only
                # and this has to work on a portable database too
                concepts_t.c.properties["component"].as_string() == component,
                concepts_t.c.code != code,
            )
            .limit(limit)
        ).all()
        if not rows:
            return f"No other specimen carries {component!r} in the loaded catalog."
        variants = [
            {
                "loinc": row._mapping["code"],
                "display": row._mapping["display"],
                "system": (row._mapping["properties"] or {}).get("system"),
            }
            for row in rows
        ]
        return json.dumps({"component": component, "variants": variants}, indent=1)

    return [loinc_search, loinc_lookup, loinc_specimen_variants]
