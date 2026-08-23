"""The agent's RxNorm toolset.

Four questions a medication conversation actually asks — what is this
drug, what is it made of, what brands exist, and what code should this
order carry — each a thin wrapper over :class:`OntologyService` and
:mod:`hdh.modules.rxnorm.coding`.

The rule these follow is design §7: **an agent tool may not contain a
decision that a non-agent caller would also need.** The level a coding
stops at, the refusal to substitute a strength, the preference for a
single-ingredient product — all of that lives in ``coding.resolve`` and is
reached identically by ``hdh rxnorm code``. A tool that grew its own copy
would make the agent's answers and the CLI's diverge, and only one of them
would be tested.
"""

from __future__ import annotations

import json

from sqlalchemy import select


def build_rxnorm_tools(session) -> list:
    """The agent's RxNorm tools bound to an open session (empty if no catalog)."""
    from anthropic import beta_tool

    from hdh.core.models import Base, tool_guard
    from hdh.modules.rxnorm.coding import resolve
    from hdh.modules.rxnorm.ontology import build_service

    guard = tool_guard(session)
    concepts_t = Base.metadata.tables["ontology_concepts"]
    if (
        session.execute(select(concepts_t.c.id).where(concepts_t.c.ontology == "rxnorm").limit(1)).first()
        is None
    ):
        return []  # catalog not loaded — don't offer tools that can only fail
    service = build_service(session)

    def _describe(concept) -> dict:
        return {
            "rxcui": concept.code,
            "level": (concept.properties or {}).get("tty"),
            "display": concept.display,
        }

    @beta_tool
    @guard
    def rxnorm_search(mention: str, levels: str = "", limit: int = 5) -> str:
        """Map a drug name to ranked RxNorm concepts. Pass the name as written in the note. Optionally constrain by term type — IN is the ingredient, SCD a clinical drug, SBD a branded drug — which sharpens ranking when you already know how specific the mention is.

        Args:
            mention: The drug as written, e.g. "lisinopril" or "Zestril 10 mg".
            levels: Comma-separated term types to prefer, e.g. "SCD,SBD".
            limit: Maximum candidates to return.
        """
        context: dict = {"limit": limit}
        if levels:
            context["levels"] = [level.strip() for level in levels.split(",") if level.strip()]
        candidates = service.normalize(mention, context)
        if not candidates:
            return "No matches in the loaded RxNorm catalog."
        return json.dumps(
            [{**_describe(c.concept), "score": c.score, "why": c.reason} for c in candidates],
            indent=1,
        )

    @beta_tool
    @guard
    def rxnorm_code_drug(mention: str, strength: str = "", route: str = "") -> str:
        """Code a drug the way the chart would: at the deepest level the evidence supports, branded when the name is a brand. Returns the RXCUI, the level it stopped at, and WHY — including when it refuses, which it does rather than guess a strength or a form the note did not give.

        Args:
            mention: The drug phrase as written, e.g. "Metformin ER 2 x 500mg".
            strength: The strength if you have it separately, e.g. "500 MG".
            route: The route if stated, e.g. "PO" or "oral".
        """
        coding = resolve(
            service,
            mention.split()[0] if mention.split() else mention,
            strength=strength or None,
            route=route or None,
            raw=mention,
        )
        if coding is None:
            return (
                "No confident match — leave the order uncoded. An uncoded order is "
                "legitimate state; a wrong RxCUI is not."
            )
        return json.dumps(
            {
                "rxcui": coding.rxcui,
                "level": coding.tty,
                "display": coding.display,
                "evidence": list(coding.evidence),
            },
            indent=1,
        )

    @beta_tool
    @guard
    def rxnorm_ingredients(rxcui: str) -> str:
        """What a drug is made of. Use this before adding a medication to check the patient is not already on one of its ingredients under another name — a combination product contains drugs it never names.

        Args:
            rxcui: The RxNorm concept identifier.
        """
        if service.lookup(rxcui) is None:
            return f"No RxNorm concept {rxcui} in the loaded catalog."
        ingredients = service.ingredients_of(rxcui)
        if not ingredients:
            return f"{rxcui} has no ingredient recorded (it may already BE an ingredient)."
        return json.dumps([_describe(c) for c in ingredients], indent=1)

    @beta_tool
    @guard
    def rxnorm_brands(rxcui: str) -> str:
        """The branded products built from a clinical drug — what a patient may know their medication by.

        Args:
            rxcui: The RxNorm concept identifier of a clinical drug.
        """
        if service.lookup(rxcui) is None:
            return f"No RxNorm concept {rxcui} in the loaded catalog."
        brands = service.brands_of(rxcui)
        if not brands:
            return f"No branded products recorded for {rxcui}."
        return json.dumps([_describe(c) for c in brands], indent=1)

    return [rxnorm_search, rxnorm_code_drug, rxnorm_ingredients, rxnorm_brands]
