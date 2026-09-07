"""Official licensed crosswalk loaders (issue #86, part 2).

The derived authorities (`ontology/derive.py`) infer ICD→SNOMED edges; this
loads an *official* map a licensee supplies as a file, through the house
`LoadStage` pipeline. The synthetic fixtures stand in for the NLM ICD-10-CM↔
SNOMED CT map and a LOINC↔SNOMED map — the licensed files never ship, so the
parser is exercised against their format and the match stage against a small
loaded catalog (the synthetic SNOMED fixture + a few hand-built concepts).
"""

from pathlib import Path

import pytest
from sqlalchemy import insert, select

from hdh.core.models import Base, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.ontology.crosswalk import LoadError, run_load

CROSSWALK = Path(__file__).parent / "fixtures" / "crosswalk"
SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
NLM_MAP = CROSSWALK / "ICD10CM_SNOMED_MAP_US_20260301.txt"
LOINC_MAP = CROSSWALK / "loinc_snomed_map_20260301.txt"


def _insert_concepts(session, ontology: str, *codes: str) -> None:
    concepts_t = Base.metadata.tables["ontology_concepts"]
    session.execute(
        insert(concepts_t),
        [
            {
                "id": f"{ontology}:{code}",
                "ontology": ontology,
                "code": code,
                "kind": "code",
                "display": f"{ontology} {code}",
                "properties": {},
            }
            for code in codes
        ],
    )
    session.commit()


def _maps_to(session, authority: str):
    edges_t = Base.metadata.tables["ontology_edges"]
    return session.execute(
        select(edges_t).where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == authority)
    ).all()


@pytest.fixture()
def loaded_db(tmp_path):
    """Synthetic SNOMED catalog + the ICD-10-CM/LOINC source concepts the
    fixtures map onto. icd10cm:E11.9 is present but snomed_ct:44054006 is NOT
    (it isn't in the SNOMED fixture) — exactly the real "map newer than the
    loaded catalog" case the match stage has to skip."""
    from hdh.modules.snomed.loader import run_load as load_snomed

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "crosswalk.db"))
    session = get_session(engine)
    load_snomed(session, SNOMED_FIXTURES)
    _insert_concepts(session, "icd10cm", "B99.9", "M19.90", "E11.9")
    _insert_concepts(session, "loinc", "11111-1", "11111-2")
    yield session
    session.close()
    engine.dispose()


def test_nlm_crosswalk_creates_official_edges_and_skips_the_rest(loaded_db):
    report = dict(run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed"))
    edges = {(e.source_id, e.target_id): e for e in _maps_to(loaded_db, "NLM_UMLS")}

    # the two pairs whose BOTH concepts are loaded become edges
    assert ("icd10cm:B99.9", "snomed_ct:100006006") in edges
    assert ("icd10cm:M19.90", "snomed_ct:100007002") in edges
    assert len(edges) == 2

    # a one-to-one row is full confidence; a one-to-many row is weaker and says so
    one_to_one = edges[("icd10cm:B99.9", "snomed_ct:100006006")]
    one_to_many = edges[("icd10cm:M19.90", "snomed_ct:100007002")]
    assert one_to_one.confidence == 1.0 and one_to_one.properties["one_to_one"] is True
    assert one_to_many.confidence == pytest.approx(0.8) and one_to_many.properties["one_to_one"] is False
    assert one_to_one.properties["display"] == "Blorbitis (disorder)"

    # E11.9's snomed target isn't loaded → skipped by cause; Z99.9's icd
    # source was never inserted → skipped; R50.9 has a blank target → blank
    assert "skipped 1 with no icd10cm concept, 1 with no snomed_ct concept" in report["match"]
    assert "1 blank rows skipped" in report["parse"]


def test_dotless_icd_codes_are_dotted_to_match_the_catalog(loaded_db):
    """NLM carries B999; the loaded concept is icd10cm:B99.9. Without the
    normalisation the pair would never match and the map would load empty."""
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")
    sources = {e.source_id for e in _maps_to(loaded_db, "NLM_UMLS")}
    assert "icd10cm:B99.9" in sources and "icd10cm:B999" not in sources


def test_a_utf8_bom_header_still_infers_and_loads(loaded_db, tmp_path):
    """PowerShell's `Set-Content -Encoding utf8` and Excel both prepend a BOM.
    Left unstripped it corrupts the first header cell ('﻿ICD_CODE') and
    inference matches nothing — the file loads via utf-8-sig either way."""
    bom = tmp_path / "ICD10CM_SNOMED_MAP_BOM_20260301.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + NLM_MAP.read_bytes())
    run_load(loaded_db, bom)  # no map_name: inference must survive the BOM
    assert len(_maps_to(loaded_db, "NLM_UMLS")) == 2


def test_the_map_is_inferred_from_the_header(loaded_db):
    """--map may be omitted: the header columns identify the spec."""
    run_load(loaded_db, NLM_MAP)  # no map_name
    assert len(_maps_to(loaded_db, "NLM_UMLS")) == 2


def test_reload_is_refused_without_force_and_replaces_with_it(loaded_db):
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")
    with pytest.raises(LoadError, match="already loaded"):
        run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")
    # --force replaces wholesale, not doubles
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed", force=True)
    assert len(_maps_to(loaded_db, "NLM_UMLS")) == 2


def test_a_foreign_authority_survives_the_load(loaded_db):
    """The official load rebuilds only its own authority. A derived edge — or
    any other authority's — must be left exactly as it was."""
    edges_t = Base.metadata.tables["ontology_edges"]
    loaded_db.execute(
        insert(edges_t),
        [
            {
                "source_id": "icd10cm:B99.9",
                "target_id": "snomed_ct:100006006",
                "edge_type": "maps_to",
                "authority": "PACK_AUTHORED",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
    loaded_db.commit()
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed", force=True)
    assert len(_maps_to(loaded_db, "PACK_AUTHORED")) == 1
    assert len(_maps_to(loaded_db, "NLM_UMLS")) == 2


def test_the_load_writes_exactly_one_ledger_row_per_map(loaded_db):
    loads_t = Base.metadata.tables["ontology_loads"]
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed", force=True)
    rows = loaded_db.execute(select(loads_t).where(loads_t.c.ontology == "icd10cm_snomed")).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.fiscal_year == 202603 and row.edge_count == 2
    assert row.properties["authority"] == "NLM_UMLS" and row.properties["parsed"] == 4


def test_loinc_map_loads_through_the_same_pipeline(loaded_db):
    run_load(loaded_db, LOINC_MAP, map_name="loinc-snomed")
    edges = {(e.source_id, e.target_id) for e in _maps_to(loaded_db, "LOINC_SNOMED")}
    assert ("loinc:11111-1", "snomed_ct:100006006") in edges
    assert ("loinc:11111-2", "snomed_ct:100009004") in edges
    # a LOINC load leaves the ICD authority empty — different maps, different edges
    assert _maps_to(loaded_db, "NLM_UMLS") == []


def test_icd_lookup_shows_the_official_mapping(loaded_db, capsys):
    from hdh.modules.icd10cm.cli import _cmd_lookup

    # the icd side needs a real catalog row to look up, with the fields the
    # lookup prints
    concepts_t = Base.metadata.tables["ontology_concepts"]
    loaded_db.execute(
        concepts_t.update()
        .where(concepts_t.c.id == "icd10cm:B99.9")
        .values(
            display="Other specified infectious diseases", is_billable=True, path="B99.9", hierarchy_depth=0
        )
    )
    loaded_db.commit()
    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")

    _cmd_lookup(loaded_db, "B99.9")
    printed = capsys.readouterr().out
    assert "maps to" in printed
    assert "snomed_ct:100006006" in printed
    assert "NLM_UMLS" in printed  # the authority is shown — official vs derived


def test_crosswalk_status_reports_live_edge_counts(loaded_db, capsys):
    from hdh.modules.ontology.cli import _cmd_crosswalk_status

    _cmd_crosswalk_status(loaded_db)
    assert "No official crosswalk loaded" in capsys.readouterr().out

    run_load(loaded_db, NLM_MAP, map_name="nlm-icd10cm-snomed")
    _cmd_crosswalk_status(loaded_db)
    out = capsys.readouterr().out
    assert "nlm-icd10cm-snomed" in out and "2 now live" in out


# ── parser guards (DB-free) ──────────────────────────────────────────────


def test_unknown_map_name_is_rejected(loaded_db):
    with pytest.raises(LoadError, match="unknown map"):
        run_load(loaded_db, NLM_MAP, map_name="no-such-map")


def test_a_header_missing_a_required_column_is_fatal(loaded_db, tmp_path):
    bad = tmp_path / "broken_20260301.txt"
    bad.write_text("ICD_CODE\tICD_NAME\nB999\tGlimmer fever\n", encoding="utf-8")
    with pytest.raises(LoadError, match="missing required column"):
        run_load(loaded_db, bad, map_name="nlm-icd10cm-snomed")


def test_a_release_that_cannot_be_detected_demands_one(loaded_db, tmp_path):
    nameless = tmp_path / "icd10cm_snomed_map.txt"
    nameless.write_text(NLM_MAP.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(LoadError, match="cannot detect a release"):
        run_load(loaded_db, nameless, map_name="nlm-icd10cm-snomed")
    # passing one explicitly works
    assert run_load(loaded_db, nameless, map_name="nlm-icd10cm-snomed", release=202603)
