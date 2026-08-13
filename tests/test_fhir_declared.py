"""Schema-registry ``fhir`` hints → generic declared-entity export (design §7).

A throwaway module declares a flat SmokingStatus entity with a ``fhir``
hint; the registry must capture and validate the hint at bootstrap, and
``DeclaredEntityEmitter`` must export rows as conformant typed resources
inside the patient bundle — with zero core edits.
"""

import json

import pytest

from hdh.core.schema_registry import SchemaError, SchemaRegistry

SMOKING_HINT = {
    "resourceType": "Observation",
    "patient_link": "patient_id",
    "set": {
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "72166-2", "display": "Tobacco smoking status"}]
        },
    },
    "fields": {
        "status_text": "valueCodeableConcept.text",
        "recorded_date": "effectiveDateTime",
    },
    "id_fields": ["recorded_date"],
}

SMOKING_SPEC = {
    "entity": "SmokingStatus",
    "tablename": "smoking_status",
    "columns": [
        {"name": "id", "type": "Integer", "primary_key": True},
        {"name": "patient_id", "type": "Integer", "foreign_key": "patients.id", "nullable": False},
        {"name": "status_text", "type": "String", "length": 80},
        {"name": "recorded_date", "type": "Date"},
    ],
    "fhir": SMOKING_HINT,
}


def make_module(root, name, *, entities=()):
    """Write a throwaway schema module directory."""
    mod = root / name
    (mod / "schema" / "entities").mkdir(parents=True)
    (mod / "manifest.json").write_text(json.dumps({"name": name, "version": "1.0"}))
    for i, spec in enumerate(entities):
        (mod / "schema" / "entities" / f"e{i}.json").write_text(json.dumps(spec))
    return mod


def test_fhir_hint_is_captured_and_described(tmp_path):
    reg = SchemaRegistry()
    reg.register_module(str(make_module(tmp_path, "mod_smoke", entities=[SMOKING_SPEC])))
    reg.load_all()
    hint, module = reg.merged_entities["SmokingStatus"]["fhir"]
    assert hint["resourceType"] == "Observation"
    assert module == "mod_smoke"
    assert "FHIR Observation" in reg.describe()


def test_fhir_hint_on_base_entity_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    spec = {"entity": "Visit", "extends": "base", "columns": [], "fhir": SMOKING_HINT}
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="hand-written emitter"):
        reg.load_all()


def test_fhir_hint_referencing_unknown_column_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    spec = dict(SMOKING_SPEC, fhir=dict(SMOKING_HINT, patient_link="person_id"))
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="unknown column"):
        reg.load_all()


def test_fhir_hint_missing_required_key_is_hard_error(tmp_path):
    reg = SchemaRegistry()
    hint = {k: v for k, v in SMOKING_HINT.items() if k != "patient_link"}
    spec = dict(SMOKING_SPEC, fhir=hint)
    reg.register_module(str(make_module(tmp_path, "bad", entities=[spec])))
    with pytest.raises(SchemaError, match="missing 'patient_link'"):
        reg.load_all()


@pytest.fixture()
def declared_registry(tmp_path, db_session, monkeypatch):
    """Apply the SmokingStatus module for real and make it THE process
    registry for the duration of the test (monkeypatch restores after)."""
    reg = SchemaRegistry()
    reg.register_module(str(make_module(tmp_path, "mod_smoke", entities=[SMOKING_SPEC])))
    reg.load_all()
    classes = reg.apply()
    cls = classes["SmokingStatus"]
    cls.__table__.create(db_session.get_bind(), checkfirst=True)
    monkeypatch.setattr("hdh.core.schema_registry.registry", reg)
    return reg


def test_declared_entity_exports_conformant_typed_resource(db_session, declared_registry):
    """End-to-end §7: rows of a hinted entity appear in the bundle as
    validated typed resources with stable ids — zero core edits."""
    from datetime import date

    from hdh.core.fhir import build_bundle
    from hdh.core.models import Patient

    patient = db_session.query(Patient).order_by(Patient.id).first()
    cls = declared_registry.new_classes["SmokingStatus"]
    db_session.add(cls(patient_id=patient.id, status_text="Former smoker", recorded_date=date(2026, 1, 10)))
    db_session.commit()

    bundle = build_bundle(patient, strict=True)
    smoking = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Observation"
        and any(c.get("code") == "72166-2" for c in e["resource"].get("code", {}).get("coding", []))
    ]
    assert len(smoking) == 1
    resource = smoking[0]
    assert resource["status"] == "final"
    assert resource["valueCodeableConcept"]["text"] == "Former smoker"
    assert resource["effectiveDateTime"] == "2026-01-10"
    assert resource["subject"]["reference"] == f"Patient/{patient.mrn}"
    # stable content-hash id: a re-export yields the identical id
    rebuilt = build_bundle(patient, strict=True)
    ids = {r["id"] for r in smoking}
    rebuilt_ids = {
        e["resource"]["id"]
        for e in rebuilt["entry"]
        if e["resource"]["resourceType"] == "Observation" and "72166-2" in json.dumps(e["resource"])
    }
    assert ids == rebuilt_ids
