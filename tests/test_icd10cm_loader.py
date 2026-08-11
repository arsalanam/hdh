"""Milestone B tests: the ICD-10-CM load pipeline over the fixture slice.

The fixture (tests/fixtures/icd10cm) is a curated ~120-row subset of the
CMS order file in its real fixed-width layout — the design doc's worked
families (E11, G89, M25.5, S52.00x, S82.5/S82.6). These tests prove the
pipeline end-to-end: parsing, hierarchy, description-derived laterality,
episode and displacement axes, edges, verification, idempotency, and the
Diagnosis link.
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from hdh.core.models import Base, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.icd10cm.loader import LoadError, run_load

FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"


def _t(name: str):
    return Base.metadata.tables[name]


@pytest.fixture()
def loaded_session(tmp_path):
    """A fresh database with the fixture slice fully loaded."""
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "icd_loader.db"))
    session = get_session(engine)
    run_load(session, FIXTURES, 2026)
    yield session
    session.close()
    engine.dispose()


def _concept(session, code: str):
    t = _t("ontology_concepts")
    return session.execute(select(t).where(t.c.code == code)).mappings().one()


def test_load_counts_and_ledger(loaded_session):
    """119 file rows + 4 chapters land; the ledger records the load."""
    t = _t("ontology_concepts")
    total = loaded_session.execute(select(func.count()).select_from(t)).scalar()
    billable = loaded_session.execute(select(func.count()).select_from(t).where(t.c.is_billable)).scalar()
    chapters = loaded_session.execute(
        select(func.count()).select_from(t).where(t.c.kind == "chapter")
    ).scalar()
    blocks = loaded_session.execute(select(func.count()).select_from(t).where(t.c.kind == "block")).scalar()
    assert (total, billable, chapters, blocks) == (128, 87, 4, 5)

    ledger = loaded_session.execute(select(_t("ontology_loads"))).mappings().all()
    assert len(ledger) == 1
    assert ledger[0]["concept_count"] == 128
    assert "icd10cm-order-2026.txt" in ledger[0]["source_checksums"]


def test_hierarchy_path_and_descendants(loaded_session):
    """S52.001A sits five deep; anchored path prefix finds the S52 family."""
    row = _concept(loaded_session, "S52.001A")
    assert row["path"] == "ch19.S50-S59.S52.S520.S5200.S52001.S52001A"
    assert row["hierarchy_depth"] == 6

    t = _t("ontology_concepts")
    s52 = _concept(loaded_session, "S52")
    descendants = loaded_session.execute(
        select(func.count()).select_from(t).where(t.c.path.like(s52["path"] + ".%"))
    ).scalar()
    assert descendants == 26  # S52.0 + S52.00 + 3 stems + 21 seventh-char codes

    parent_edges = loaded_session.execute(
        select(func.count())
        .select_from(_t("ontology_edges"))
        .where(_t("ontology_edges").c.edge_type == "parent_of")
    ).scalar()
    assert parent_edges == 124  # +5 blocks  # every non-chapter concept has exactly one


def test_laterality_from_descriptions(loaded_session):
    """Sides come from the words, not character positions — S52 encodes
    laterality at char 6, S82.5x at char 5; both group correctly."""
    right = _concept(loaded_session, "S52.001A")
    left = _concept(loaded_session, "S52.002A")
    unspec = _concept(loaded_session, "S52.009A")
    assert (right["laterality"], left["laterality"], unspec["laterality"]) == ("1", "2", "9")
    assert right["laterality_group"] == left["laterality_group"] == unspec["laterality_group"]

    ankle_right = _concept(loaded_session, "S82.51XA")
    ankle_left = _concept(loaded_session, "S82.52XA")
    assert (ankle_right["laterality"], ankle_left["laterality"]) == ("1", "2")
    assert ankle_right["properties"]["axes"]["laterality"] == "right"

    # standalone "unspecified" without sided siblings must NOT lateralize
    e119 = _concept(loaded_session, "E11.40")  # "...neuropathy, unspecified"
    assert e119["laterality"] is None


def test_contralateral_edges(loaded_session):
    """lateral(S82.52XA) → S82.51XA, both directions stored."""
    t, e = _t("ontology_concepts"), _t("ontology_edges")
    left = _concept(loaded_session, "S82.52XA")
    other = loaded_session.execute(
        select(t.c.code)
        .join(e, e.c.target_id == t.c.id)
        .where(e.c.source_id == left["id"], e.c.edge_type == "contralateral")
    ).scalar_one()
    assert other == "S82.51XA"


def test_displacement_axis_variant(loaded_session):
    """S82.52XA (displaced) ⇄ S82.55XA (nondisplaced), same side and site."""
    t, e = _t("ontology_concepts"), _t("ontology_edges")
    displaced = _concept(loaded_session, "S82.52XA")
    assert displaced["properties"]["axes"]["displacement"] == "displaced"
    variant = loaded_session.execute(
        select(t.c.code)
        .join(e, e.c.target_id == t.c.id)
        .where(e.c.source_id == displaced["id"], e.c.edge_type == "axis_variant")
    ).scalar_one()
    assert variant == "S82.55XA"


def test_episode_variants(loaded_session):
    """The S52.001 stem fans out to its seven 7th-character encounters."""
    row = _concept(loaded_session, "S52.001A")
    assert row["episode"] == "A" and row["episode_group"] == "S52.001"
    e = _t("ontology_edges")
    stem = _concept(loaded_session, "S52.001")
    fan_out = loaded_session.execute(
        select(func.count())
        .select_from(e)
        .where(e.c.source_id == stem["id"], e.c.edge_type == "episode_variant")
    ).scalar()
    assert fan_out == 7


def test_reload_requires_force(loaded_session):
    """A second load of the same FY refuses without force, succeeds with."""
    with pytest.raises(LoadError, match="already loaded"):
        run_load(loaded_session, FIXTURES, 2026)
    report = run_load(loaded_session, FIXTURES, 2026, force=True)
    assert dict(report)["load"].startswith("128")
    ledger = loaded_session.execute(select(_t("ontology_loads"))).mappings().all()
    assert len(ledger) == 2


def test_link_diagnoses(loaded_session):
    """hdh icd link backfills concept_id for codes present in the catalog."""
    from datetime import date

    from hdh.core.models import Diagnosis, Patient, Sex, Visit, VisitType
    from hdh.modules.icd10cm.cli import _cmd_link

    patient = Patient(
        mrn="MRN10000001",
        first_name="Link",
        last_name="Case",
        date_of_birth=date(1955, 3, 3),
        sex=Sex.FEMALE,
    )
    visit = Visit(patient=patient, visit_date=date(2026, 2, 2), visit_type=VisitType.FOLLOW_UP)
    in_catalog = Diagnosis(visit=visit, icd10_code="E11.9", description="T2DM")
    not_in_catalog = Diagnosis(visit=visit, icd10_code="J06.9", description="URI")
    loaded_session.add_all([patient, visit, in_catalog, not_in_catalog])
    loaded_session.commit()

    _cmd_link(loaded_session)
    assert in_catalog.concept_id == "icd10cm:E11.9"
    assert not_in_catalog.concept_id is None


def test_corrupt_file_aborts_before_ledger(tmp_path):
    """A bad row fails parse; nothing is written, no ledger entry appears."""
    bootstrap_schema()
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "icd10cm-order-2026.txt").write_text("garbage line\n", encoding="utf-8")
    engine = get_engine(str(tmp_path / "corrupt.db"))
    session = get_session(engine)
    with pytest.raises(LoadError):
        run_load(session, bad_dir, 2026)
    assert session.execute(select(func.count()).select_from(_t("ontology_loads"))).scalar() == 0
    session.close()
    engine.dispose()


# ─── Milestone C: tabular XML — blocks, rule edges, sevenChrDef ─────────────


def test_blocks_between_chapter_and_category(loaded_session):
    """The XML's sections become block concepts; categories re-parent."""
    block = _concept(loaded_session, "S50-S59")
    assert block["kind"] == "block" and block["hierarchy_depth"] == 1
    assert block["display"].startswith("Injuries to the elbow")

    t, e = _t("ontology_concepts"), _t("ontology_edges")
    s52 = _concept(loaded_session, "S52")
    parent = loaded_session.execute(
        select(t.c.code)
        .join(e, e.c.source_id == t.c.id)
        .where(e.c.target_id == s52["id"], e.c.edge_type == "parent_of")
    ).scalar_one()
    assert parent == "S50-S59"


def test_rule_edges_resolved_and_noted(loaded_session):
    """Excludes2 with an in-catalog ref becomes an edge; out-of-catalog
    refs keep their note on properties but produce no edge."""
    t, e = _t("ontology_concepts"), _t("ontology_edges")
    s52 = _concept(loaded_session, "S52")
    excl2 = loaded_session.execute(
        select(t.c.code, e.c.properties)
        .join(e, e.c.target_id == t.c.id)
        .where(e.c.source_id == s52["id"], e.c.edge_type == "excludes2")
    ).all()
    assert [(c, p["note"]) for c, p in excl2] == [("S82", "fracture at ankle level (S82.-)")]

    notes = s52["properties"]["notes"]
    assert notes["excludes1"] == ["traumatic amputation of forearm (S58.-)"]
    excl1_edges = loaded_session.execute(
        select(func.count()).select_from(e).where(e.c.source_id == s52["id"], e.c.edge_type == "excludes1")
    ).scalar()
    assert excl1_edges == 0

    e11 = _concept(loaded_session, "E11")
    assert e11["properties"]["notes"]["use_additional"] == ["code to identify control using insulin (Z79.4)"]


def test_seven_chr_def_drives_episodes(loaded_session):
    """Episodes validate against the family's sevenChrDef from the XML."""
    row = _concept(loaded_session, "S82.51XB")
    assert row["episode"] == "B"  # B is in S82's fixture defs


def test_non_encounter_seventh_char_is_not_an_episode():
    """A fetus-digit sevenChrDef (obstetrics) must not classify as an
    episode — the full-catalog lesson baked into a unit test."""
    from hdh.modules.icd10cm.loader import LoadContext
    from hdh.modules.icd10cm.loader.stages import EnrichStage
    from hdh.modules.icd10cm.loader.tabular import TabularData

    ctx = LoadContext(session=None, source_dir=Path("."), fiscal_year=2026)
    ctx.tabular = TabularData(seven_defs={"O31": {"1": "fetus 1", "0": "not applicable or unspecified"}})
    ctx.concepts = {
        "icd10cm:O31.32X1": {
            "code": "O31.32X1",
            "kind": "code",
            "display": "Continuing pregnancy after fetal death, second trimester, fetus 1",
        }
    }
    assert EnrichStage._episodes(ctx) == 0
    assert "episode" not in ctx.concepts["icd10cm:O31.32X1"]
