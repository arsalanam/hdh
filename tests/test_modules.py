import pytest

from hdh.core.models import Patient


def test_care_gap_detection(db_session):
    from hdh.modules.caregaps import detect_gaps, reference_date

    as_of = reference_date(db_session)
    gaps = detect_gaps(db_session, as_of=as_of)
    for g in gaps:
        assert g.severity in ("high", "medium", "low")
        assert g.mrn.startswith("MRN")
    # sorted most-severe first
    ranks = [("high", "medium", "low").index(g.severity) for g in gaps]
    assert ranks == sorted(ranks)


def test_risk_features(db_session):
    pytest.importorskip("numpy")
    from hdh.modules.caregaps import reference_date
    from hdh.modules.risk.features import FEATURE_NAMES, extract_features

    mrns, rows, labels = extract_features(db_session, cutoff=reference_date(db_session))
    assert len(mrns) == len(rows) == len(labels) == 8
    assert all(len(r) == len(FEATURE_NAMES) for r in rows)
    assert set(labels) <= {0, 1}


def test_soap_narrative(db_session):
    from hdh.modules.narrative import patient_soap_notes

    p = db_session.query(Patient).first()
    notes = patient_soap_notes(p, last_n=2)
    assert notes
    for note in notes:
        for section in ("S:", "O:", "A:", "P:"):
            assert section in note


def test_billing_scaffold(db_session):
    from hdh.modules.billing import estimate_claim

    p = db_session.query(Patient).filter(Patient.visits.any()).first()
    claim = estimate_claim(p.visits[0], p.age)
    assert claim["cpt"].startswith("99")
    assert claim["estimated_charge_usd"] > 0


def test_ontology_scaffold():
    from hdh.modules.ontology import snomed_for_icd10

    assert snomed_for_icd10("I10") == ("59621000", "Essential hypertension")
    assert snomed_for_icd10("Z99.99") is None


def test_agent_tools_build(db_session):
    pytest.importorskip("anthropic")
    from hdh.modules.agent.tools import build_tools

    tools = build_tools(db_session)
    assert {t.name for t in tools} == {
        "get_patient_chart",
        "search_patients",
        "get_care_gaps",
        "get_risk_scores",
        "query_database",
        "dataset_stats",
    }


def test_fhir_api_app(db_session):
    pytest.importorskip("fastapi")
    from hdh.modules.fhir_api.server import create_app

    app = create_app(db_path=":memory:")
    paths = {r.path for r in app.routes}
    assert "/metadata" in paths
    assert "/Patient/{mrn}" in paths
