"""Full US Edition property tests (design snomed-module.md §9) — golden
concepts and scale invariants that only make sense against a REAL,
licensee-loaded edition.

CI never touches licensed data: every test here is skipped unless
``HDH_SNOMED_DB_URL`` points at a database where `hdh snomed load
--download` has completed (e.g. the `just deps` PostgreSQL). Run with:

    HDH_SNOMED_DB_URL=postgresql+psycopg://... uv run pytest -m fullload
"""

import os

import pytest

pytestmark = pytest.mark.fullload

# Golden SCTIDs (well-known identifiers; the licensed content stays in the DB)
DIABETES = "73211009"  # Diabetes mellitus (disorder)
STROKE = "230690007"  # Cerebrovascular accident (disorder)
DISEASE = "64572001"  # Disease (disorder)
THROMBECTOMY = "43810009"  # Thrombectomy (procedure)
CLINICAL_FINDING = "404684003"


@pytest.fixture(scope="module")
def catalog():
    url = os.environ.get("HDH_SNOMED_DB_URL")
    if not url:
        pytest.skip("HDH_SNOMED_DB_URL not set — full-edition tests run only on a licensee's machine")
    from hdh.core.models import get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(db_url=url)
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def service(catalog):
    from hdh.core.ontology import get_ontology_service

    service = get_ontology_service("snomed_ct", catalog)
    if service.lookup(DIABETES) is None:
        pytest.skip("SNOMED US Edition not loaded in HDH_SNOMED_DB_URL")
    return service


def test_golden_concepts_present(service):
    diabetes = service.lookup(DIABETES)
    assert diabetes.properties["semantic_tag"] == "disorder"
    assert "diabetes" in diabetes.display.lower()
    assert service.lookup(STROKE) is not None
    assert service.lookup(THROMBECTOMY) is not None


def test_golden_subsumption(service):
    assert service.subsumes(DISEASE, DIABETES)
    assert service.subsumes(CLINICAL_FINDING, STROKE)
    assert not service.subsumes(DIABETES, DISEASE)


def test_thrombectomy_carries_method_attribute(service):
    grouped = service.attributes(THROMBECTOMY)
    assert grouped, "thrombectomy should carry defining attributes"
    assert "method" in grouped


def test_scale_within_tolerance(catalog):
    from sqlalchemy import func, select

    from hdh.core.models import Base

    tables = Base.metadata.tables
    concepts = catalog.execute(
        select(func.count())
        .select_from(tables["ontology_concepts"])
        .where(tables["ontology_concepts"].c.ontology == "snomed_ct")
    ).scalar()
    terms = catalog.execute(select(func.count()).select_from(tables["ontology_terms"])).scalar()
    closure = catalog.execute(select(func.count()).select_from(tables["ontology_closure"])).scalar()
    assert concepts > 300_000, f"only {concepts:,} concepts — expected the full US Edition"
    assert terms > 1_000_000, f"only {terms:,} terms"
    assert closure > 3_000_000, f"only {closure:,} closure rows"


def test_descendant_sweep_is_nontrivial(service):
    diabetes_family = service.descendants(DIABETES)
    assert len(diabetes_family) > 50  # T1DM, T2DM, and the long tail
    codes = {c.code for c in diabetes_family}
    assert "44054006" in codes  # Type 2 diabetes mellitus
