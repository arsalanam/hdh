"""Catalog-derived ICD→SNOMED tagging (issue #29).

Three sources under precedence (profile-authored, curated demo map,
normalize()-derived from the loaded SNOMED catalog), Condition
backfilling, and authority-tagged maps_to edges that never touch other
authorities' rows. The synthetic SNOMED fixture stands in for the
licensed catalog — the derivation only cares about rows, not provenance."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from hdh.core.models import Condition, ConditionStatus, Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.ontology.derive import (
    derive_mappings,
    record_maps_to_edges,
    tag_conditions,
)

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"


def _patient_with(session, *conditions: tuple[str, str]) -> Patient:
    patient = Patient(
        mrn=f"MRN{abs(hash(conditions)) % 10**8:08d}",
        first_name="Tag",
        last_name="Case",
        date_of_birth=date(1970, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    for icd10, description in conditions:
        session.add(
            Condition(
                patient_id=patient.id,
                icd10_code=icd10,
                description=description,
                chronic=True,
                status=ConditionStatus.ACTIVE,
                onset_date=date(2024, 1, 1),
            )
        )
    session.commit()
    return patient


@pytest.fixture()
def tagging_db(tmp_path):
    """A database with the synthetic SNOMED catalog + hand-built conditions."""
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "derive.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    yield session
    session.close()
    engine.dispose()


def test_three_sources_with_precedence(tagging_db):
    import sys

    sys.path.insert(0, str(SNOMED_FIXTURES))
    import fixture_ids as fx

    _patient_with(
        tagging_db,
        ("N18.4", "Chronic kidney disease, stage 4"),  # profile-authored (CKD stage)
        # curated demo map. NOT J06.9 any more: uri_adult became
        # profile-authored when #86 completed catalog coverage, and profile
        # outranks curated — same SNOMED code, higher-precedence source.
        ("F32.9", "Depressive disorder"),
        ("B99.9", "Glimmer fever"),  # derived: matches a fixture synonym
        ("Q99.999", "Unmappable synthetic mystery"),  # stays unmapped
    )
    mappings = derive_mappings(tagging_db)
    assert mappings["N18.4"].source == "profile" and mappings["N18.4"].snomed_code == "431857002"
    assert mappings["F32.9"].source == "curated" and mappings["F32.9"].snomed_code == "35489007"
    assert mappings["B99.9"].source == "derived" and mappings["B99.9"].snomed_code == fx.BLORBITIS
    assert mappings["B99.9"].confidence >= 0.6
    assert "Q99.999" not in mappings

    # profile-authored beats the curated map where both know the code
    _patient_with(tagging_db, ("I10", "Essential hypertension"))
    assert derive_mappings(tagging_db)["I10"].source == "profile"


def test_tagging_backfills_and_reports_by_source(tagging_db):
    _patient_with(
        tagging_db,
        ("E11.9", "Type 2 diabetes mellitus without complications"),
        ("B99.9", "Glimmer fever"),
    )
    counts = tag_conditions(tagging_db, derive_mappings(tagging_db))
    assert counts["profile"] == 1 and counts["derived"] == 1
    tagged = {
        c.icd10_code: c.snomed_code
        for c in tagging_db.query(Condition).filter(Condition.snomed_code.isnot(None))
    }
    assert tagged["E11.9"] == "44054006"
    # re-run: nothing new to tag
    assert sum(tag_conditions(tagging_db, derive_mappings(tagging_db)).values()) == 0


def test_maps_to_edges_only_for_resolvable_concepts_and_idempotent(tagging_db):
    from hdh.core.models import Base

    _patient_with(tagging_db, ("B99.9", "Glimmer fever"), ("E11.9", "Type 2 diabetes"))
    mappings = derive_mappings(tagging_db)
    # B99.9's snomed target EXISTS (fixture) but icd10cm:B99.9 does not
    # (no ICD catalog here) — so no edge rows at all in this database
    assert record_maps_to_edges(tagging_db, mappings) == 0

    # a foreign-authority maps_to edge must survive our rebuilds
    edges_t = Base.metadata.tables["ontology_edges"]
    concepts_t = Base.metadata.tables["ontology_concepts"]
    some = [row[0] for row in tagging_db.execute(select(concepts_t.c.id).limit(2))]
    from sqlalchemy import insert

    tagging_db.execute(
        insert(edges_t),
        [
            {
                "source_id": some[0],
                "target_id": some[1],
                "edge_type": "maps_to",
                "authority": "OFFICIAL_MAP",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    tagging_db.commit()
    record_maps_to_edges(tagging_db, mappings)  # rebuild our authorities
    survivors = tagging_db.execute(select(edges_t.c.authority).where(edges_t.c.edge_type == "maps_to")).all()
    assert ("OFFICIAL_MAP",) in survivors


def test_fail_soft_without_snomed_catalog(tmp_path):
    """No loaded SNOMED catalog: profile + curated still tag; derived is
    silently empty (the funnel finds nothing, never raises)."""
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "bare.db"))
    session = get_session(engine)
    _patient_with(session, ("E11.9", "Type 2 diabetes"), ("B99.9", "Glimmer fever"))
    mappings = derive_mappings(session)
    assert mappings["E11.9"].source == "profile"
    assert "B99.9" not in mappings
    counts = tag_conditions(session, mappings)
    assert counts["profile"] == 1 and counts["derived"] == 0
    session.close()
    engine.dispose()


# ── catalog coverage and the chartable/not-chartable line (#86) ──────────


def test_every_condition_maps_to_snomed():
    """Coverage is the point of #86: an ICD code with no SNOMED equivalent
    cannot be reconciled, cross-walked, or reasoned about by the agent."""
    from hdh.core.conditions import default_catalog

    catalog = default_catalog()
    unmapped = [n for n in catalog.names() if not catalog.get(n).snomed_code]
    assert not unmapped, f"conditions with no SNOMED code: {sorted(unmapped)}"


def test_every_mapped_code_records_its_hierarchy():
    """The code alone does not say whether it is a problem. Whoever authors
    one has to say which hierarchy it came from, because the consumer
    downstream cannot look it up — a FHIR enricher has no session."""
    from hdh.core.conditions import default_catalog
    from hdh.modules.ontology.fhir import CHARTABLE_TAGS

    catalog = default_catalog()
    known = CHARTABLE_TAGS | {"procedure", "event"}
    for name in catalog.names():
        profile = catalog.get(name)
        if profile.snomed_code:
            assert profile.snomed_tag in known, f"{name}: unknown snomed_tag {profile.snomed_tag!r}"


def test_encounter_reasons_are_mapped_but_not_problems():
    """The decision this issue turned on.

    An annual physical, a well-child visit and a fall all map correctly to
    SNOMED — as `procedure` and `event`. Mapping them keeps ICD→SNOMED
    coverage complete; refusing to call them problems keeps them off the
    problem list.
    """
    from hdh.core.conditions import default_catalog
    from hdh.modules.ontology.fhir import CHARTABLE_TAGS

    catalog = default_catalog()
    by_name = {n: catalog.get(n) for n in catalog.names()}

    for name in ("annual_physical_adult", "well_child", "sports_physical", "fall_injury"):
        profile = by_name[name]
        assert profile.snomed_code, f"{name} should still MAP"
        assert profile.snomed_tag not in CHARTABLE_TAGS, f"{name} is not a problem"

    # and the ordinary case is untouched
    assert by_name["type2_diabetes"].snomed_tag == "disorder"
    # a history-of IS a problem-list entry, and SNOMED calls it a situation
    assert by_name["stroke_history"].snomed_tag == "situation"
    assert by_name["stroke_history"].snomed_tag in CHARTABLE_TAGS


def test_the_enricher_skips_a_non_problem_and_keeps_a_problem():
    """`Condition.code` is bound to problems and diagnoses. A procedure
    concept in it would be a conformance error dressed as completeness."""
    from types import SimpleNamespace

    from hdh.modules.ontology.fhir import ConditionCodingEnricher

    enricher = ConditionCodingEnricher()

    def condition_resource():
        return SimpleNamespace(code=SimpleNamespace(coding=[]))

    # an encounter reason: mapped, but not appended
    skipped = condition_resource()
    enricher.enrich(
        skipped,
        SimpleNamespace(
            snomed_code="162673000",
            snomed_display="General examination of patient",
            description="Annual physical",
        ),
        None,
    )
    assert skipped.code.coding == [], "a procedure concept reached Condition.code"

    # an ordinary disorder: appended as before
    charted = condition_resource()
    enricher.enrich(
        charted,
        SimpleNamespace(
            snomed_code="44054006", snomed_display="Type 2 diabetes mellitus", description="Type 2 diabetes"
        ),
        None,
    )
    assert [c.code for c in charted.code.coding] == ["44054006"]


def test_icd_lookup_answers_from_the_icd_side(tagging_db, capsys):
    """`maps_to` edges existed since `hdh ontology tag`, and only
    comprehension read them — from the SNOMED side, to find a billing code.
    Asked from the ICD side there was no answer at all (#86)."""
    from sqlalchemy import insert

    from hdh.core.models import Base
    from hdh.modules.icd10cm.cli import _cmd_lookup

    tables = Base.metadata.tables
    tagging_db.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": "icd10cm:E11.9",
                "ontology": "icd10cm",
                "code": "E11.9",
                "kind": "code",
                "display": "Type 2 diabetes mellitus without complications",
                "is_billable": True,
                "path": "E11.9",
                "hierarchy_depth": 0,
                "properties": {},
            }
        ],
    )
    tagging_db.execute(
        insert(tables["ontology_edges"]),
        [
            {
                "source_id": "icd10cm:E11.9",
                "target_id": "snomed_ct:44054006",
                "edge_type": "maps_to",
                "authority": "PACK_AUTHORED",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    tagging_db.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": "snomed_ct:44054006",
                "ontology": "snomed_ct",
                "code": "44054006",
                "kind": "concept",
                "display": "Type 2 diabetes mellitus",
                "properties": {},
            }
        ],
    )
    tagging_db.flush()

    _cmd_lookup(tagging_db, "E11.9")
    printed = capsys.readouterr().out
    assert "maps to" in printed
    assert "snomed_ct:44054006" in printed
    # the authority is shown, because an asserted mapping and a derived one
    # are different things to trust
    assert "PACK_AUTHORED" in printed
