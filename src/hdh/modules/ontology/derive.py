"""Catalog-derived ICD-10→SNOMED mappings (issue #29).

Three sources, in precedence order — each one a different kind of truth:

1. **Profile-authored** (confidence 1.0): the generator's own
   ConditionProfiles carry SNOMED codes since the clinical-breadth arc —
   for generated data, the pack author's code IS the answer.
2. **Curated** (confidence 1.0): the demo ``ICD10_TO_SNOMED`` hand map,
   kept as explicit curation for codes the profiles don't cover.
3. **Derived** (confidence = funnel score): the SNOMED module's
   ``normalize()`` funnel over the loaded catalog, constrained to
   disorder/finding semantic tags and accepted above a threshold. Only
   available when a licensee has loaded the US Edition; fail-soft
   otherwise.

Everything used is a published surface: the core catalog
(``default_catalog``) and the core ``OntologyService`` protocol — no
module internals cross the boundary. Mappings also materialize as
``maps_to`` edges (authority-tagged, confidence-carrying, idempotent per
authority) when both concepts exist in the shared tables — the first
real rows of the cross-ontology plan (issue #18): derived edges never
pretend to be official crosswalks.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, insert, select

DERIVE_THRESHOLD = 0.6  # minimum funnel score to accept a derived mapping
DERIVE_TAGS = ("disorder", "finding")  # the mention kinds a Condition can be

# authority strings on the maps_to edges this module owns (and may rebuild)
AUTHORITIES = ("PACK_AUTHORED", "CURATED_DEMO", "DERIVED_NORMALIZE")


@dataclass(frozen=True)
class Mapping:
    """One ICD-10→SNOMED mapping with its provenance."""

    icd10_code: str
    snomed_code: str
    snomed_display: str
    source: str  # "profile" | "curated" | "derived"
    confidence: float


def derive_mappings(session) -> dict[str, Mapping]:
    """The mapping table for every distinct ICD-10 code on generated
    conditions, from all three sources under precedence."""
    from hdh.core.models import Condition

    codes = [code for (code,) in session.query(Condition.icd10_code).distinct()]
    mappings: dict[str, Mapping] = {}
    _add_profile_mappings(mappings)
    _add_curated_mappings(mappings)
    _add_derived_mappings(session, mappings, [c for c in codes if c not in mappings])
    return {code: m for code, m in mappings.items() if code in set(codes)}


def _add_profile_mappings(mappings: dict[str, Mapping]) -> None:
    """Source 1: SNOMED codes authored on the generator's own profiles."""
    from hdh.core.conditions import default_catalog

    catalog = default_catalog()
    for name in catalog.names():
        profile = catalog.get(name)
        if profile.snomed_code and profile.icd10_code not in mappings:
            mappings[profile.icd10_code] = Mapping(
                icd10_code=profile.icd10_code,
                snomed_code=profile.snomed_code,
                snomed_display=profile.description,
                source="profile",
                confidence=1.0,
            )
        if profile.staging:  # staged codes map too (CKD 3a/3b/4/5)
            for stage in profile.staging.stages:
                if stage.snomed_code and stage.icd10_code not in mappings:
                    mappings[stage.icd10_code] = Mapping(
                        icd10_code=stage.icd10_code,
                        snomed_code=stage.snomed_code,
                        snomed_display=stage.description,
                        source="profile",
                        confidence=1.0,
                    )


def _add_curated_mappings(mappings: dict[str, Mapping]) -> None:
    """Source 2: the demo hand map, as explicit curation."""
    from hdh.modules.ontology import ICD10_TO_SNOMED

    for icd10, (snomed_id, display) in ICD10_TO_SNOMED.items():
        if icd10 not in mappings:
            mappings[icd10] = Mapping(
                icd10_code=icd10,
                snomed_code=snomed_id,
                snomed_display=display,
                source="curated",
                confidence=1.0,
            )


def _add_derived_mappings(session, mappings: dict[str, Mapping], unmapped: list[str]) -> None:
    """Source 3: the SNOMED normalize() funnel over the loaded catalog,
    seeded with each code's own condition description text."""
    from hdh.core.models import Condition
    from hdh.core.ontology import get_ontology_service

    if not unmapped:
        return
    service = get_ontology_service("snomed_ct", session)
    for icd10 in unmapped:
        description = session.query(Condition.description).filter(Condition.icd10_code == icd10).first()
        if description is None or not description[0]:
            continue
        candidates = service.normalize(description[0], {"semantic_tags": list(DERIVE_TAGS), "limit": 1})
        if candidates and candidates[0].score >= DERIVE_THRESHOLD:
            concept = candidates[0].concept
            mappings[icd10] = Mapping(
                icd10_code=icd10,
                snomed_code=concept.code,
                snomed_display=concept.display,
                source="derived",
                confidence=round(candidates[0].score, 3),
            )


def tag_conditions(session, mappings: dict[str, Mapping]) -> dict[str, int]:
    """Backfill Condition.snomed_code/_display for untagged rows; returns
    per-source tag counts."""
    from hdh.core.models import Condition

    if not hasattr(Condition, "snomed_code"):  # registry-injected column
        raise RuntimeError("ontology schema module not bootstrapped — no snomed_code column")
    counts = {"profile": 0, "curated": 0, "derived": 0}
    for mapping in mappings.values():
        updated = (
            session.query(Condition)
            .filter(Condition.icd10_code == mapping.icd10_code, Condition.snomed_code.is_(None))
            .update({"snomed_code": mapping.snomed_code, "snomed_display": mapping.snomed_display})
        )
        counts[mapping.source] += updated
    session.commit()
    return counts


def record_maps_to_edges(session, mappings: dict[str, Mapping]) -> int:
    """Materialize mappings as maps_to edges where BOTH concepts exist in
    the shared tables. Idempotent per authority: this module's edges are
    rebuilt wholesale; official crosswalk edges (issue #18, other
    authorities) are never touched."""
    from hdh.core.models import Base

    tables = Base.metadata.tables
    concepts_t, edges_t = tables["ontology_concepts"], tables["ontology_edges"]
    known = {
        row[0]
        for row in session.execute(
            select(concepts_t.c.id).where(concepts_t.c.ontology.in_(("icd10cm", "snomed_ct")))
        )
    }
    authority_for = {"profile": "PACK_AUTHORED", "curated": "CURATED_DEMO", "derived": "DERIVED_NORMALIZE"}
    session.execute(
        delete(edges_t).where(edges_t.c.edge_type == "maps_to", edges_t.c.authority.in_(AUTHORITIES))
    )
    rows = []
    for mapping in mappings.values():
        source_id = f"icd10cm:{mapping.icd10_code}"
        target_id = f"snomed_ct:{mapping.snomed_code}"
        if source_id in known and target_id in known:
            rows.append(
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_type": "maps_to",
                    "authority": authority_for[mapping.source],
                    "confidence": mapping.confidence,
                    "properties": {"source": mapping.source},
                }
            )
    if rows:
        session.execute(insert(edges_t), rows)
    session.commit()
    return len(rows)
