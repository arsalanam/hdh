"""The OntologyService protocol + icd10cm as implementation #1
(snomed-module.md milestone A / notes-comprehension-service.md §4–§5).

Proves the encapsulation contract before the first DAG rows exist:
dispatch works, tree answers come through the protocol, and the two
loud-failure paths (unknown ontology, tree math on a path-less concept)
raise instead of returning silently empty."""

from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.ontology import Concept, OntologyService, UnsupportedHierarchy, get_ontology_service
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.icd10cm.loader import run_load

FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """The ICD fixture slice, loaded once for all protocol tests."""
    bootstrap_schema()
    db = tmp_path_factory.mktemp("ontology") / "catalog.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    run_load(session, FIXTURES, 2026)
    yield session
    session.close()
    engine.dispose()


def test_dispatcher_returns_icd_service(catalog):
    service = get_ontology_service("icd10cm", catalog)
    assert isinstance(service, OntologyService)
    assert service.ontology == "icd10cm"


def test_dispatcher_unknown_ontology_is_loud(catalog):
    with pytest.raises(LookupError, match="rxnorm"):
        get_ontology_service("rxnorm", catalog)


def test_lookup_returns_typed_concept(catalog):
    concept = get_ontology_service("icd10cm", catalog).lookup("S52.001")
    assert isinstance(concept, Concept)
    assert concept.ontology == "icd10cm"
    assert concept.id == "icd10cm:S52.001"
    assert "ulna" in concept.display.lower()
    assert get_ontology_service("icd10cm", catalog).lookup("Z99.999") is None


def test_ancestors_are_the_chapter_chain(catalog):
    ancestors = get_ontology_service("icd10cm", catalog).ancestors("S52.001")
    codes = [c.code for c in ancestors]
    assert "S52" in codes and "S52.0" in codes and "S52.00" in codes
    assert all(isinstance(c, Concept) for c in ancestors)


def test_descendants_cover_the_subtree(catalog):
    service = get_ontology_service("icd10cm", catalog)
    codes = {c.code for c in service.descendants("S52.00")}
    assert {"S52.001", "S52.002", "S52.009"} <= codes
    assert "S52.00" not in codes  # excludes itself


def test_subsumes_is_one_prefix_test(catalog):
    service = get_ontology_service("icd10cm", catalog)
    assert service.subsumes("S52", "S52.001")
    assert not service.subsumes("S52.001", "S52")
    assert not service.subsumes("S82.5", "S52.001")


def test_synonyms_and_normalize(catalog):
    service = get_ontology_service("icd10cm", catalog)
    assert service.synonyms("S52.001")  # at least the display
    candidates = service.normalize("fracture ulna")
    assert candidates and candidates[0].score >= candidates[-1].score
    assert all(c.concept.ontology == "icd10cm" for c in candidates)


def test_tree_math_on_pathless_concept_raises_not_empty(catalog):
    """§5 item 3: a DAG-ontology row (path NULL by contract) must fail
    LOUDLY when it reaches the tree helpers, never match silently nothing."""
    from sqlalchemy import insert

    from hdh.core.models import Base
    from hdh.modules.icd10cm.ontology import descendant_ids

    concepts_t = Base.metadata.tables["ontology_concepts"]
    catalog.execute(
        insert(concepts_t).values(
            id="snomed_ct:404684003",
            ontology="snomed_ct",
            code="404684003",
            kind="concept",
            display="Clinical finding",
        )
    )
    catalog.commit()
    with pytest.raises(UnsupportedHierarchy, match="materialized path"):
        descendant_ids(catalog, ["snomed_ct:404684003"])


def test_new_entities_and_enum_values_registered(db_session):
    """Milestone A schema: ontology_terms/ontology_closure exist and the
    shared enums learned 'concept' / 'attribute'."""
    from hdh.core.models import Base

    tables = Base.metadata.tables
    assert "ontology_terms" in tables and "ontology_closure" in tables
    assert set(tables["ontology_closure"].primary_key.columns.keys()) == {"ancestor_id", "descendant_id"}
    assert "concept" in tables["ontology_concepts"].c.kind.type.enums
    assert "attribute" in tables["ontology_edges"].c.edge_type.type.enums
