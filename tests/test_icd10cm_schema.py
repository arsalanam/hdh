"""Milestone A tests for the ICD-10-CM module's persistence tier.

The module ships no Python model code — three entities, their indexes, and
the Diagnosis bridge exist purely as ``schema/*.json`` materialized by the
registry (design §3.2). These tests prove the JSON is the schema: tables,
indexes, double-FK edge relationships, the Diagnosis link, and the
existing-database upgrade path.
"""

import sqlite3

from sqlalchemy import inspect, select

from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema


def _classes():
    registry = bootstrap_schema()
    return registry, registry.new_classes


def test_module_registered_after_ontology():
    """icd10cm_module loads after its ontology_module dependency."""
    registry, new_classes = _classes()
    order = [m.name for m in registry._ordered_modules()]
    assert order.index("ontology_module") < order.index("icd10cm_module")
    assert {"OntologyConcept", "OntologyEdge", "OntologyLoad"} <= set(new_classes)


def test_tables_and_indexes_materialize(tmp_path):
    """The JSON specs produce real tables with the declared indexes."""
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "icd.db"))
    inspector = inspect(engine)
    assert {"ontology_concepts", "ontology_edges", "ontology_loads"} <= set(inspector.get_table_names())
    concept_indexes = {ix["name"]: ix for ix in inspector.get_indexes("ontology_concepts")}
    assert concept_indexes["ux_concept_ontology_code"]["unique"]
    assert set(concept_indexes) >= {
        "ux_concept_ontology_code",
        "ix_concept_path",
        "ix_concept_laterality_group",
        "ix_concept_episode_group",
    }
    edge_indexes = {ix["name"] for ix in inspector.get_indexes("ontology_edges")}
    assert {"ix_edge_source_type", "ix_edge_target_type"} <= edge_indexes
    engine.dispose()


def test_graph_roundtrip_contralateral(tmp_path):
    """Two concepts + a contralateral edge: the double-FK relationships
    resolve to the right sides (the design's S52.001A/S52.002A example)."""
    _registry, new_classes = _classes()
    Concept, Edge = new_classes["OntologyConcept"], new_classes["OntologyEdge"]

    engine = get_engine(str(tmp_path / "graph.db"))
    session = get_session(engine)
    right = Concept(
        id="icd10cm:S52.001A",
        ontology="icd10cm",
        code="S52.001A",
        kind="code",
        display="Unspecified fracture of upper end of right ulna, initial encounter",
        laterality="1",
        laterality_group="s52.001:fracture upper end ulna",
        is_billable=True,
    )
    left = Concept(
        id="icd10cm:S52.002A",
        ontology="icd10cm",
        code="S52.002A",
        kind="code",
        display="Unspecified fracture of upper end of left ulna, initial encounter",
        laterality="2",
        laterality_group="s52.001:fracture upper end ulna",
        is_billable=True,
        properties={"axes": {"laterality": "left"}},
    )
    session.add_all([right, left])
    session.add(
        Edge(
            source_id=right.id,
            target_id=left.id,
            edge_type="contralateral",
            authority="DERIVED_LOADER",
        )
    )
    session.commit()

    edge = session.execute(select(Edge).filter_by(edge_type="contralateral")).scalar_one()
    assert edge.source.laterality == "1" and edge.target.laterality == "2"
    assert edge.confidence == 1.0  # server_default applied
    assert edge.target.properties == {"axes": {"laterality": "left"}}

    variants = session.query(Concept).filter_by(laterality_group="s52.001:fracture upper end ulna").count()
    assert variants == 2
    session.close()
    engine.dispose()


def test_diagnosis_concept_bridge(tmp_path):
    """Diagnosis.concept_id (registry-added) links a chart row to the graph."""
    _registry, new_classes = _classes()
    Concept = new_classes["OntologyConcept"]
    from datetime import date

    from hdh.core.models import Condition, Patient, Sex, Visit, VisitType

    engine = get_engine(str(tmp_path / "bridge.db"))
    session = get_session(engine)
    concept = Concept(
        id="icd10cm:E11.9",
        ontology="icd10cm",
        code="E11.9",
        kind="code",
        display="Type 2 diabetes mellitus without complications",
        is_billable=True,
    )
    patient = Patient(
        mrn="MRN00000001",
        first_name="Bridge",
        last_name="Test",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.MALE,
    )
    visit = Visit(patient=patient, visit_date=date(2026, 1, 5), visit_type=VisitType.FOLLOW_UP)
    dx = Condition(
        patient=patient,
        visit=visit,
        icd10_code="E11.9",
        description="Type 2 diabetes mellitus",
        concept_id="icd10cm:E11.9",
    )
    session.add_all([concept, patient, visit, dx])
    session.commit()

    stored = session.query(Condition).filter_by(icd10_code="E11.9").one()
    assert stored.concept.display.startswith("Type 2 diabetes")
    session.close()
    engine.dispose()


def test_existing_database_upgrade_path(tmp_path):
    """A database created before this module existed gains the new tables
    (create_all) and the concept_id column (ensure_columns) on open."""
    bootstrap_schema()
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conditions (id INTEGER PRIMARY KEY, patient_id INTEGER, visit_id INTEGER, "
        "icd10_code VARCHAR(10), description VARCHAR(200), chronic BOOLEAN, "
        "status VARCHAR(9), controlled BOOLEAN, is_primary BOOLEAN, onset_date DATE, resolved_date DATE)"
    )
    conn.execute(
        "INSERT INTO conditions (patient_id, visit_id, icd10_code, description, status) "
        "VALUES (1, 1, 'E11.9', 'T2DM', 'ACTIVE')"
    )
    conn.commit()
    conn.close()

    engine = get_engine(str(db))
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("conditions")}
    assert {"concept_id", "snomed_code", "snomed_display"} <= cols  # both modules applied
    assert "ontology_concepts" in inspector.get_table_names()
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT icd10_code FROM conditions").scalar() == "E11.9"
    engine.dispose()
