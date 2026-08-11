"""ICD-10-CM tools for the care-program agent (design §8.1).

This is a **published inter-module API**: the agent module consumes it via
``build_icd_tools`` (guarded — the agent works without this module). Inside
the agent there is no nested LLM call: the agent itself performs axis
extraction and passes terms + stated axes as tool arguments; the funnel's
deterministic stages do the rest, and the pipeline validator can ground
every code the answer cites in these results.
"""

from __future__ import annotations

import json

from sqlalchemy import select


def build_icd_tools(session) -> list:
    """The agent's ICD toolset bound to an open session (empty if no catalog)."""
    from anthropic import beta_tool

    from hdh.core.models import Base

    concepts_t = Base.metadata.tables["ontology_concepts"]
    if session.execute(select(concepts_t.c.id).limit(1)).first() is None:
        return []  # catalog not loaded — don't offer tools that can only fail
    edges_t = Base.metadata.tables["ontology_edges"]

    @beta_tool
    def icd_codify(  # quality: allow(no-god-class) — flat params ARE the tool schema the model fills; one per clinical axis
        terms: str,
        laterality: str = "",
        aspect: str = "",
        displacement: str = "",
        encounter: str = "",
        exposure: str = "",
        limit: int = 5,
    ) -> str:
        """Find ICD-10-CM codes for a clinical description. YOU do the extraction: pass formal clinical terms (medial malleolus, not "inner ankle") and ONLY the axes the description actually states — omit unstated axes entirely, never guess. Candidates return with matched/conflicting/unstated axes; if the top result has unstated axes, ask the user about them.

        Args:
            terms: Formal clinical search terms, e.g. "fracture medial malleolus".
            laterality: right | left | bilateral — only if stated.
            aspect: medial | lateral | anterior | posterior — only if stated.
            displacement: displaced | nondisplaced — only if stated.
            encounter: initial | subsequent | sequela — only if stated.
            exposure: open | closed — only if stated.
            limit: Maximum candidates to return.
        """
        from hdh.modules.icd10cm.service import ambiguous_axes, codify, stub_extractor

        axes = {
            k: v
            for k, v in {
                "laterality": laterality,
                "aspect": aspect,
                "displacement": displacement,
                "encounter": encounter,
                "exposure": exposure,
            }.items()
            if v
        }
        extraction, candidates = codify(session, terms, stub_extractor(terms, axes), limit=limit)
        return json.dumps(
            {
                "understood_axes": extraction.axes,
                "ask_about": list(ambiguous_axes(candidates, extraction.axes)),
                "candidates": [
                    {
                        "code": c.code,
                        "display": c.display,
                        "matched": list(c.matched),
                        "conflicts": list(c.conflicts),
                        "unstated": list(c.unstated),
                    }
                    for c in candidates
                ],
            },
            indent=1,
        )

    @beta_tool
    def icd_lookup(code: str) -> str:
        """Full context for one ICD-10-CM code: hierarchy, axes, laterality variants, coding-rule notes.

        Args:
            code: A dotted ICD-10-CM code, e.g. S82.52XA or E11.9.
        """
        row = (
            session.execute(
                select(concepts_t).where(
                    concepts_t.c.ontology == "icd10cm", concepts_t.c.code == code.upper()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return f"Code '{code}' not found in the loaded catalog."
        related = session.execute(
            select(edges_t.c.edge_type, concepts_t.c.code)
            .join(concepts_t, edges_t.c.target_id == concepts_t.c.id)
            .where(
                edges_t.c.source_id == row["id"],
                edges_t.c.edge_type.in_(("contralateral", "axis_variant", "excludes1", "excludes2")),
            )
        ).all()
        out = {
            "code": row["code"],
            "display": row["display"],
            "billable": row["is_billable"],
            "path": row["path"],
            "axes": (row["properties"] or {}).get("axes", {}),
            "notes": (row["properties"] or {}).get("notes", {}),
            "related": [{"edge": e, "code": c} for e, c in related],
        }
        return json.dumps(out, indent=1)

    @beta_tool
    def icd_search(term: str, limit: int = 10) -> str:
        """Search ICD-10-CM code descriptions (full-text; most specific first).

        Args:
            term: Clinical search words, e.g. "fracture forearm".
            limit: Maximum rows.
        """
        from hdh.modules.icd10cm.cli import search_concepts

        rows = search_concepts(session, term, limit)
        return (
            json.dumps([{"code": c, "display": d, "billable": b} for c, d, b in rows], indent=1)
            if rows
            else "No matches."
        )

    @beta_tool
    def icd_pattern(pattern_json: str) -> str:
        """Run a graph-pattern query over the ICD-10-CM knowledge graph. Pattern is JSON with: anchor {terms|code}, optional traverse [{edge: parent_of|contralateral|axis_variant|episode_variant|excludes1|excludes2|code_first|use_additional, depth: "*" for parent_of descendants}], optional axes {laterality|aspect|displacement|encounter|exposure: value}, optional constraints {billable: true}. Example: all left-sided billable codes under S52.0: {"anchor":{"code":"S52.0"},"traverse":[{"edge":"parent_of","depth":"*"}],"axes":{"laterality":"left"},"constraints":{"billable":true}}. On a validation error, fix the pattern per the message and retry.

        Args:
            pattern_json: The pattern as a JSON string.
        """
        from hdh.modules.icd10cm.patterns import PatternError, run_pattern

        try:
            pattern = json.loads(pattern_json)
        except json.JSONDecodeError as err:
            return f"Pattern is not valid JSON: {err}"
        try:
            hits = run_pattern(session, pattern)
        except PatternError as err:
            return f"Invalid pattern: {err}"
        return (
            json.dumps([{"code": h.code, "display": h.display} for h in hits], indent=1)
            if hits
            else "No matches."
        )

    return [icd_codify, icd_lookup, icd_search, icd_pattern]
