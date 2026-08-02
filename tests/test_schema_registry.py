"""Tests for the schema registry — the extensible-schema design realized.

Covers the design's collision rules (docs/design/original-design-notes.md §9)
with throwaway modules built in tmp_path, plus the shipped ontology module
end-to-end: bootstrap → generate → tag → export carries SNOMED codings.
"""

import json

import pytest

from hdh.core.schema_registry import SchemaError, SchemaRegistry


def make_module(root, name, *, depends_on=(), entities=(), relationships=(), priority=10):
    """Write a throwaway schema module directory."""
    mod = root / name
    (mod / "schema" / "entities").mkdir(parents=True)
    (mod / "schema" / "relationships").mkdir(parents=True)
    (mod / "manifest.json").write_text(
        json.dumps({"name": name, "version": "1.0", "depends_on": list(depends_on), "priority": priority})
    )
    for i, spec in enumerate(entities):
        (mod / "schema" / "entities" / f"e{i}.json").write_text(json.dumps(spec))
    for i, spec in enumerate(relationships):
        (mod / "schema" / "relationships" / f"r{i}.json").write_text(json.dumps(spec))
    return mod


def test_later_module_wins_on_column_collision(tmp_path, caplog):
    reg = SchemaRegistry()
    col = {"name": "note_flag", "type": "String", "length": 10}
    spec = {"entity": "Visit", "extends": "base", "columns": [col]}
    reg.register_module(str(make_module(tmp_path, "mod_a", entities=[spec])))
    reg.register_module(str(make_module(tmp_path, "mod_b", depends_on=["mod_a"], entities=[dict(spec)])))
    with caplog.at_level("WARNING", logger="hdh.schema"):
        reg.load_all()
    assert "later module wins" in caplog.text
    assert reg.merged_entities["Visit"]["columns"]["note_flag"][1] == "mod_b"


def test_redeclaring_base_column_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    spec = {
        "entity": "Visit",
        "extends": "base",
        "columns": [{"name": "visit_date", "type": "Date"}],
    }
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="re-declares a base column"):
        reg.load_all()


def test_tablename_rename_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    spec = {"entity": "Visit", "tablename": "encounters", "extends": "base", "columns": []}
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="may not rename"):
        reg.load_all()


def test_relationship_to_unknown_entity_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    rel = {
        "entity": "Visit",
        "relationships": [{"name": "ghosts", "target": "Ghost", "type": "one_to_many"}],
    }
    reg.register_module(str(make_module(tmp_path, "bad", relationships=[rel])))
    with pytest.raises(SchemaError, match="unknown entity Ghost"):
        reg.load_all()


def test_circular_dependency_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    reg.register_module(str(make_module(tmp_path, "mod_a", depends_on=["mod_b"])))
    reg.register_module(str(make_module(tmp_path, "mod_b", depends_on=["mod_a"])))
    with pytest.raises(SchemaError, match="circular"):
        reg.load_all()


def test_new_entity_requires_tablename(tmp_path):
    reg = SchemaRegistry()
    spec = {"entity": "CarePlan", "columns": [{"name": "id", "type": "Integer", "primary_key": True}]}
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="no tablename"):
        reg.load_all()


def test_describe_reports_order_and_extensions(tmp_path):
    reg = SchemaRegistry()
    spec = {
        "entity": "Visit",
        "extends": "base",
        "columns": [{"name": "note_flag", "type": "String", "length": 10}],
    }
    reg.register_module(str(make_module(tmp_path, "mod_a", entities=[spec])))
    reg.load_all()
    out = reg.describe()
    assert "base → mod_a" in out
    assert "Visit [extends base]" in out
    assert "note_flag (mod_a)" in out


def test_ontology_module_end_to_end(db_session):
    """The shipped extension: snomed columns exist, tag fills them, FHIR emits them."""
    from hdh.core.exporters import patient_to_fhir_bundle
    from hdh.core.models import Diagnosis, Patient
    from hdh.modules.ontology import ICD10_TO_SNOMED

    # conftest bootstraps the registry, so the extension columns exist
    assert hasattr(Diagnosis, "snomed_code")

    # backfill (what `hdh ontology tag` does)
    for icd10, (snomed_id, display) in ICD10_TO_SNOMED.items():
        db_session.query(Diagnosis).filter(
            Diagnosis.icd10_code == icd10, Diagnosis.snomed_code.is_(None)
        ).update({"snomed_code": snomed_id, "snomed_display": display})
    db_session.commit()

    tagged = db_session.query(Diagnosis).filter(Diagnosis.snomed_code.isnot(None)).first()
    assert tagged is not None, "tiny panel should still contain at least one mapped diagnosis"

    patient = db_session.query(Patient).filter(Patient.id == tagged.visit.patient_id).one()
    bundle = patient_to_fhir_bundle(patient)
    snomed_codings = [
        coding
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "Condition"
        for coding in entry["resource"]["code"]["coding"]
        if coding["system"] == "http://snomed.info/sct"
    ]
    assert snomed_codings, "FHIR Conditions should carry the SNOMED coding"
