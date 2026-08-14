"""SNOMED CT tools for the care-program agent (design snomed-module.md §7).

A **published inter-module API**, consumed by the agent module via
``build_snomed_tools`` exactly like ``build_icd_tools`` — guarded, so the
agent works without this module or its catalog. The agent performs its
own mention extraction and passes plain arguments; every tool below is
deterministic SQL over the loaded catalog, so the pipeline validator can
ground each cited SCTID in these results.
"""

from __future__ import annotations

import json

from sqlalchemy import select


def build_snomed_tools(session) -> list:
    """The agent's SNOMED toolset bound to an open session (empty if no catalog)."""
    from anthropic import beta_tool

    from hdh.core.models import tool_guard

    guard = tool_guard(session)

    from hdh.core.models import Base
    from hdh.modules.snomed.ontology import build_service

    concepts_t = Base.metadata.tables["ontology_concepts"]
    if (
        session.execute(select(concepts_t.c.id).where(concepts_t.c.ontology == "snomed_ct").limit(1)).first()
        is None
    ):
        return []  # catalog not loaded — don't offer tools that can only fail
    service = build_service(session)

    @beta_tool
    @guard
    def snomed_normalize(mention: str, semantic_tags: str = "", under: str = "", limit: int = 5) -> str:
        """Map a clinical mention to ranked SNOMED CT concepts. Pass the mention as written in the note; optionally constrain by semantic tags (what KIND of thing it is) and/or an ancestor SCTID (what subtree it should live under) — both sharpen ranking, neither is required.

        Args:
            mention: The clinical phrase, e.g. "heart attack" or "removal of flenum".
            semantic_tags: Comma-separated tags to prefer, e.g. "disorder,finding" or "procedure".
            under: An ancestor SCTID the concept should descend from, if known.
            limit: Maximum candidates to return.
        """
        context: dict = {"limit": limit}
        if semantic_tags:
            context["semantic_tags"] = [t.strip() for t in semantic_tags.split(",") if t.strip()]
        if under:
            context["ancestors"] = [under.strip()]
        candidates = service.normalize(mention, context)
        if not candidates:
            return "No matches in the loaded SNOMED CT catalog."
        return json.dumps(
            [
                {
                    "sctid": c.concept.code,
                    "display": c.concept.display,
                    "semantic_tag": c.concept.properties.get("semantic_tag"),
                    "score": c.score,
                    "why": c.reason,
                }
                for c in candidates
            ],
            indent=1,
        )

    @beta_tool
    @guard
    def snomed_lookup(sctid: str) -> str:
        """Full context for one SNOMED CT concept: FSN, semantic tag, synonyms, nearest ancestors, and defining attributes (method, finding site, ...).

        Args:
            sctid: The concept id, e.g. 73211009.
        """
        concept = service.lookup(sctid)
        if concept is None:
            return f"SCTID '{sctid}' not found in the loaded catalog."
        return json.dumps(
            {
                "sctid": concept.code,
                "display": concept.display,
                "fsn": concept.properties.get("fsn"),
                "semantic_tag": concept.properties.get("semantic_tag"),
                "synonyms": list(service.synonyms(sctid))[:8],
                "nearest_ancestors": [
                    {"sctid": a.code, "display": a.display} for a in list(service.ancestors(sctid))[:5]
                ],
                "attributes": {
                    name: [{"sctid": t.code, "display": t.display} for t in targets]
                    for name, targets in service.attributes(sctid).items()
                },
            },
            indent=1,
        )

    @beta_tool
    @guard
    def snomed_subsumes(ancestor_sctid: str, descendant_sctid: str) -> str:
        """Is one concept a kind of another? One transitive-closure hit — use this to answer "is X a form of Y" questions authoritatively instead of guessing from names.

        Args:
            ancestor_sctid: The broader concept, e.g. 64572001 (Disease).
            descendant_sctid: The narrower concept, e.g. 73211009 (Diabetes mellitus).
        """
        verdict = service.subsumes(ancestor_sctid, descendant_sctid)
        a, d = service.lookup(ancestor_sctid), service.lookup(descendant_sctid)
        return json.dumps(
            {
                "subsumes": verdict,
                "ancestor": a.display if a else None,
                "descendant": d.display if d else None,
            }
        )

    @beta_tool
    @guard
    def snomed_descendants(sctid: str, limit: int = 25) -> str:
        """Every concept under a SNOMED CT subtree (nearest first) — the closure sweep behind cohort questions like "all disorders under cerebrovascular disease".

        Args:
            sctid: The subtree root, e.g. 62914000.
            limit: Maximum rows.
        """
        rows = service.descendants(sctid)
        if not rows:
            return "No descendants (unknown SCTID or a leaf concept)."
        return json.dumps(
            [{"sctid": c.code, "display": c.display} for c in rows[:limit]],
            indent=1,
        )

    return [snomed_normalize, snomed_lookup, snomed_subsumes, snomed_descendants]
