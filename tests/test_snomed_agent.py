"""SNOMED milestone D: the normalize() funnel's ranking context, the
agent toolset (published API, catalog-gated), and the unified term index
(ICD retro-load, snomed design Q2) — all offline on synthetic fixtures."""

import json
import sys
from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.ontology import get_ontology_service
from hdh.core.schema_registry import bootstrap_schema

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
ICD_FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """Both fixture catalogs in one database — the unified search surface."""
    from hdh.modules.icd10cm.loader import run_load as icd_load
    from hdh.modules.snomed.loader import run_load as snomed_load

    bootstrap_schema()
    db = tmp_path_factory.mktemp("snomed_agent") / "catalog.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    snomed_load(session, SNOMED_FIXTURES)
    icd_load(session, ICD_FIXTURES, 2026)
    yield session
    session.close()
    engine.dispose()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


# ── funnel ranking context ───────────────────────────────────────────────────


def test_semantic_tag_fit_reranks(catalog):
    service = get_ontology_service("snomed_ct", catalog)
    plain = service.normalize("removal")
    proc = service.normalize("removal", {"semantic_tags": ["procedure"]})
    proc_tags = [c.concept.properties.get("semantic_tag") for c in proc[:2]]
    assert set(proc_tags) == {"procedure"}, "procedure tag must outrank the qualifier value"
    assert any(c.concept.properties.get("semantic_tag") == "qualifier value" for c in plain[:2])


def test_ancestor_context_boosts_subtree(catalog):
    service = get_ontology_service("snomed_ct", catalog)
    anchored = service.normalize("blorbitis", {"ancestors": [fx.DISORDER_FLENUM]})
    by_code = {c.concept.code: c for c in anchored}
    assert by_code[fx.FLENUM_BLORBITIS].score > by_code[fx.ACUTE_BLORBITIS].score
    assert "in context subtree" in by_code[fx.FLENUM_BLORBITIS].reason


# ── unified term index (design Q2) ───────────────────────────────────────────


def test_icd_load_fills_shared_term_index(catalog):
    from sqlalchemy import func, select

    from hdh.core.models import Base

    terms_t = Base.metadata.tables["ontology_terms"]
    icd_terms = catalog.execute(
        select(func.count()).select_from(terms_t).where(terms_t.c.concept_id.like("icd10cm:%"))
    ).scalar()
    assert icd_terms > 100  # the fixture slice, long + short descriptions


def test_icd_synonyms_come_from_term_index(catalog):
    from sqlalchemy import func, select

    from hdh.core.models import Base

    service = get_ontology_service("icd10cm", catalog)
    terms = service.synonyms("S52.001")
    assert terms and terms[0] == service.lookup("S52.001").display  # preferred first
    terms_t = Base.metadata.tables["ontology_terms"]
    shorts = catalog.execute(
        select(func.count())
        .select_from(terms_t)
        .where(terms_t.c.concept_id.like("icd10cm:%"), terms_t.c.term_type == "synonym")
    ).scalar()
    assert shorts > 0  # distinct short descriptions became synonym rows


def test_icd_terms_backfill_is_idempotent(catalog):
    from sqlalchemy import func, select

    from hdh.core.models import Base
    from hdh.modules.icd10cm.cli import _cmd_terms

    terms_t = Base.metadata.tables["ontology_terms"]

    def count():
        return catalog.execute(
            select(func.count()).select_from(terms_t).where(terms_t.c.concept_id.like("icd10cm:%"))
        ).scalar()

    before = count()
    _cmd_terms(catalog)
    _cmd_terms(catalog)
    assert count() == before  # replace-wholesale, never duplicate


# ── agent tools (published API) ──────────────────────────────────────────────


def test_snomed_tools_register_and_gate(catalog, tmp_path):
    from hdh.modules.snomed.agent_tools import build_snomed_tools

    names = {t.name for t in build_snomed_tools(catalog)}
    assert names == {"snomed_normalize", "snomed_lookup", "snomed_subsumes", "snomed_descendants"}

    empty_engine = get_engine(str(tmp_path / "empty.db"))
    empty = get_session(empty_engine)
    assert build_snomed_tools(empty) == []  # no catalog → no tools offered
    empty.close()
    empty_engine.dispose()


def test_agent_build_tools_includes_snomed(catalog):
    from hdh.modules.agent.tools import build_tools

    names = {t.name for t in build_tools(catalog)}
    assert {"snomed_normalize", "snomed_subsumes"} <= names
    assert {"icd_codify", "icd_lookup"} <= names  # both ontologies side by side


def test_normalize_tool_returns_grounded_json(catalog):
    from hdh.modules.snomed.agent_tools import build_snomed_tools

    normalize = _tool(build_snomed_tools(catalog), "snomed_normalize")
    payload = json.loads(normalize.call({"mention": "glimmer fever"}))
    assert payload[0]["sctid"] == fx.BLORBITIS
    assert payload[0]["semantic_tag"] == "disorder"

    constrained = json.loads(normalize.call({"mention": "removal", "semantic_tags": "procedure", "limit": 3}))
    assert all(c["semantic_tag"] == "procedure" for c in constrained[:2])


def test_subsumes_and_lookup_tools(catalog):
    from hdh.modules.snomed.agent_tools import build_snomed_tools

    tools = build_snomed_tools(catalog)
    verdict = json.loads(
        _tool(tools, "snomed_subsumes").call(
            {"ancestor_sctid": fx.BLORBITIS, "descendant_sctid": fx.SEVERE_ACUTE}
        )
    )
    assert verdict["subsumes"] is True and verdict["ancestor"] == "Blorbitis"

    context = json.loads(_tool(tools, "snomed_lookup").call({"sctid": fx.FLENUMECTOMY}))
    assert context["attributes"]["method"][0]["sctid"] == fx.REMOVAL_ACTION
    assert any(a["sctid"] == fx.REMOVAL_PROC for a in context["nearest_ancestors"])
