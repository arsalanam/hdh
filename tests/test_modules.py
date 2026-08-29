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
        # comprehension tools appear because generated data has stored notes;
        # icd/snomed tools stay gated off (no catalogs in this fixture)
        "comprehend_note",
        "get_note_record",
        "search_note_mentions",
        "apply_note",
        "amend_chart_entry",
        "void_chart_entry",
        "chart_history",
        # care planning, steered stage by stage (S4b)
        "start_care_plan",
        "show_care_plan",
        "approve_care_plan_stage",
        "amend_care_plan_stage",
        "reject_care_plan_stage",
        "show_care_plan_rubric",
        "write_care_plan_page",
    }


def test_fhir_api_app(db_session):
    pytest.importorskip("fastapi")
    from hdh.modules.fhir_api.server import create_app

    app = create_app(db_path=":memory:")
    paths = {r.path for r in app.routes}
    assert "/metadata" in paths
    assert "/Patient/{mrn}" in paths


def test_gap_finder_registry():
    from hdh.modules.caregaps import FINDERS, get_finder

    assert set(FINDERS) >= {"rules", "ai"}
    assert get_finder("rules").name == "rules"
    with pytest.raises(ValueError, match="Available: ai, rules"):
        get_finder("psychic")


def test_rule_finder_matches_detect_gaps(db_session):
    from hdh.modules.caregaps import detect_gaps, get_finder

    via_finder = get_finder("rules").find(db_session, limit=10)
    direct = detect_gaps(db_session, limit=10)
    assert [g.to_dict() for g in via_finder] == [g.to_dict() for g in direct]
    assert all(g.source == "rules" for g in via_finder)


def test_ai_finder_maps_findings_offline(db_session):
    from hdh.core.models import Patient
    from hdh.modules.caregaps.ai_finder import AIGapFinder

    seen_charts = []

    def fake_review(chart, as_of):
        seen_charts.append(chart)
        return {
            "gaps": [
                {
                    "gap_type": "missing_hba1c_monitoring",
                    "severity": "high",
                    "description": "Diabetic with no HbA1c in 14 months",
                    "recommendation": "Order HbA1c",
                },
                {
                    "gap_type": "statin_gap",
                    "severity": "medium",
                    "description": "Hyperlipidemia without statin",
                    "recommendation": "Consider statin",
                },
            ]
        }

    mrn = db_session.query(Patient).filter(Patient.visits.any()).first().mrn
    gaps = AIGapFinder(review=fake_review).find(db_session, mrn=mrn)

    assert len(gaps) == 2
    assert seen_charts and "PATIENT CHART SUMMARY" in seen_charts[0]
    assert gaps[0].severity == "high" and gaps[1].severity == "medium"  # sorted
    assert gaps[0].gap_type == "missing_hba1c_monitoring"
    assert "Order HbA1c" in gaps[0].description
    assert all(g.source == "ai" and g.mrn == mrn for g in gaps)


def test_ai_finder_samples_most_complex_patients(db_session):
    from hdh.modules.caregaps.ai_finder import AIGapFinder

    reviewed = []

    def fake_review(chart, as_of):
        reviewed.append(chart)
        return {"gaps": []}

    AIGapFinder(review=fake_review).find(db_session, sample=3)
    assert len(reviewed) <= 3


def test_ai_finder_chart_clipping_keeps_recent_visits():
    from hdh.modules.caregaps.ai_finder import _clip_chart

    chart = "HEADER\n" + "\n".join(f"VISIT {i}" for i in range(3000))
    clipped = _clip_chart(chart, 12_000)
    assert len(clipped) < 12_200
    assert clipped.startswith("HEADER")  # demographics kept
    assert "VISIT 2999" in clipped  # most recent visit kept
    assert "older visits omitted" in clipped
    assert _clip_chart("short", 12_000) == "short"
