"""Comprehension milestone A (design comprehension-extraction-schema.md):
stages 1–2 — deterministic segmentation, extraction contracts, and the
validator whose rejections are the retry feedback. All offline: the stub
extractor stands in for the LLM (the codify/stub_extractor trick)."""

import pytest

from hdh.core.notes import render_soap
from hdh.modules.comprehension.comprehend import ComprehensionError, comprehend_text, render_report
from hdh.modules.comprehension.contracts import (
    SECTION_DEFAULT_ASSERTION,
    Assertion,
    AttributeKind,
    MentionType,
    RelationKind,
    SectionKind,
)
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.comprehension.segment import segment
from hdh.modules.comprehension.validate import ExtractionError, build_extraction


class _Vital:
    bp_systolic, bp_diastolic, heart_rate = 142, 88, 112
    respiratory_rate, temperature_f, oxygen_sat = 16, 98.2, 97
    bmi, pain_scale = 27.9, 0


NOTE = render_soap(
    provider_name="Dr. Sarah Mitchell, MD",
    visit_date="2026-08-15",
    chief_complaint="Palpitations / irregular heartbeat",
    follow_up_days=90,
    age=67,
    sex="female",
    allergies=["Penicillin"],
    chronic_history=["Essential hypertension", "Chronic kidney disease, stage 3a"],
    family_history=["mother: type 2 diabetes"],
    vital=_Vital(),
    conditions=[("Unspecified atrial fibrillation", "I48.91")],
    prescriptions=[{"drug_name": "Apixaban", "dose": "5mg", "frequency": "BID", "is_new": True}],
    labs=[("INR", 2.4, "ratio", "HIGH")],
    procedures=[],
)


def _mention(type_, text, occurrence=1, attributes=()):
    return {"type": type_, "text": text, "occurrence": occurrence, "attributes": list(attributes)}


GOOD_RAW = {
    "mentions": [
        _mention("problem", "Palpitations / irregular heartbeat"),
        _mention("allergy", "Penicillin"),
        _mention("problem", "Essential hypertension"),
        _mention("problem", "Chronic kidney disease, stage 3a"),
        _mention("problem", "type 2 diabetes"),
        _mention(
            "lab_vital",
            "BP",
            1,
            [
                {"kind": "value", "text": "142/88", "occurrence": 1},
                {"kind": "unit", "text": "mmHg", "occurrence": 1},
            ],
        ),
        _mention(
            "lab_vital",
            "INR",
            1,
            [
                {"kind": "value", "text": "2.4", "occurrence": 1},
                {"kind": "interpretation", "text": "(high)", "occurrence": 1},
            ],
        ),
        _mention("problem", "Unspecified atrial fibrillation"),
        _mention(
            "medication",
            "Apixaban",
            1,
            [
                {"kind": "dose", "text": "5mg", "occurrence": 1},
                {"kind": "frequency", "text": "BID", "occurrence": 1},
                {"kind": "status_word", "text": "Start", "occurrence": 1},
            ],
        ),
    ],
    "relations": [{"kind": "treats", "source": 8, "target": 7, "inferred": True}],
    "shared_triggers": [],
}


# ── stage 1: segmentation ────────────────────────────────────────────────────


def test_segmenter_matches_the_real_note_shape():
    sections = segment(NOTE)
    kinds = [s.kind for s in sections]
    for expected in (
        SectionKind.HEADER,
        SectionKind.SUBJECTIVE,
        SectionKind.SUBJECTIVE_ALLERGY,
        SectionKind.SUBJECTIVE_HISTORY,
        SectionKind.SUBJECTIVE_FAMILY,
        SectionKind.OBJECTIVE,
        SectionKind.ASSESSMENT,
        SectionKind.PLAN,
    ):
        assert expected in kinds, expected
    for section in sections:  # spans are real substrings
        assert NOTE[section.span.start : section.span.end]


def test_unrecognized_note_falls_back_to_unknown_never_skips():
    sections = segment("Patient seen today. Doing well. Continue current meds.")
    assert [s.kind for s in sections] == [SectionKind.UNKNOWN]
    assert SECTION_DEFAULT_ASSERTION[SectionKind.UNKNOWN] is Assertion.PRESENT


# ── stage 2: the happy path ──────────────────────────────────────────────────


def test_good_extraction_validates_end_to_end():
    extraction = comprehend_text(NOTE, stub_extractor(GOOD_RAW))
    assert len(extraction.mentions) == 9
    by_text = {m.text: m for m in extraction.mentions}

    # the fifth type (review decision Q3) with section-driven default
    allergy = by_text["Penicillin"]
    assert allergy.mention_type is MentionType.ALLERGY
    assert extraction.section_of(allergy).kind is SectionKind.SUBJECTIVE_ALLERGY

    # section defaults: history vs family vs assessment
    assert extraction.section_of(by_text["Essential hypertension"]).kind is SectionKind.SUBJECTIVE_HISTORY
    assert extraction.section_of(by_text["type 2 diabetes"]).kind is SectionKind.SUBJECTIVE_FAMILY
    afib = by_text["Unspecified atrial fibrillation"]
    assert extraction.section_of(afib).kind is SectionKind.ASSESSMENT

    # verbatim invariant held everywhere
    for mention in extraction.mentions:
        assert NOTE[mention.span.start : mention.span.end] == mention.text
        for attribute in mention.attributes:
            assert NOTE[attribute.span.start : attribute.span.end] == attribute.text

    # composite mention sub-structure
    rx = by_text["Apixaban"]
    assert {a.kind for a in rx.attributes} == {
        AttributeKind.DOSE,
        AttributeKind.FREQUENCY,
        AttributeKind.STATUS_WORD,
    }

    # the TREATS relation, grounded on validated indexes
    relation = extraction.relations[0]
    assert relation.kind is RelationKind.TREATS and relation.inferred
    assert extraction.mentions[relation.source_id].text == "Apixaban"

    report = render_report(extraction)
    assert "Apixaban" in report and "treats" in report


# ── the validator's rejection classes (each reason is retry feedback) ────────


def _reject(raw) -> list[str]:
    with pytest.raises(ExtractionError) as err:
        build_extraction(NOTE, raw, segment(NOTE))
    return err.value.reasons


def test_verbatim_violation_rejected():
    reasons = _reject({"mentions": [{"type": "problem", "text": "afib", "start": 0, "end": 4}]})
    assert any("verbatim" in reason for reason in reasons)


def test_unknown_enum_values_rejected_with_allowed_list():
    reasons = _reject({"mentions": [_mention("diagnosis", "Apixaban")]})
    assert any("unknown value" in r and "problem" in r for r in reasons)


def test_illegal_attribute_kind_rejected():
    reasons = _reject(
        {
            "mentions": [
                _mention(
                    "problem", "Essential hypertension", 1, [{"kind": "dose", "text": "5mg", "occurrence": 1}]
                )
            ]
        }
    )
    assert any("illegal on a problem mention" in reason for reason in reasons)


def test_same_type_partial_overlap_rejected():
    reasons = _reject(
        {
            "mentions": [
                _mention("problem", "Chronic kidney disease"),
                _mention("problem", "kidney disease, stage 3a"),
            ]
        }
    )
    assert any("overlap" in reason for reason in reasons)


def test_contained_same_type_mention_collapses_into_the_larger():
    # live-testing admission: the model re-emits a diagnosis as its own
    # indication ("...Lisinopril for hypertension") — nested same-type
    # spans collapse deterministically, no retry burned
    raw = {
        "mentions": [
            _mention("problem", "Essential hypertension"),
            _mention("problem", "hypertension"),
            _mention("medication", "Apixaban"),
        ],
        "relations": [{"kind": "treats", "source": 2, "target": 1, "inferred": True}],
    }
    extraction = build_extraction(NOTE, raw, segment(NOTE))
    texts = [m.text for m in extraction.mentions]
    assert "Essential hypertension" in texts and "hypertension" not in texts
    relation = extraction.relations[0]
    assert extraction.mentions[relation.source_id].text == "Apixaban"
    assert extraction.mentions[relation.target_id].text == "Essential hypertension"
    for index, mention in enumerate(extraction.mentions):
        assert mention.id == index


def test_reserved_relation_kind_rejected():
    raw = {
        "mentions": [_mention("lab_vital", "INR"), _mention("problem", "Essential hypertension")],
        "relations": [{"kind": "measures", "source": 0, "target": 1, "inferred": True}],
    }
    assert any("reserved" in reason for reason in _reject(raw))


def test_relation_type_rules_enforced():
    raw = {
        "mentions": [_mention("problem", "Essential hypertension"), _mention("medication", "Apixaban")],
        "relations": [{"kind": "treats", "source": 0, "target": 1, "inferred": True}],
    }
    assert any("requires source in" in reason for reason in _reject(raw))


def test_text_not_found_rejected():
    reasons = _reject({"mentions": [_mention("problem", "Palpitations", 3)]})
    assert any("not found" in reason for reason in reasons)


# ── the retry loop ───────────────────────────────────────────────────────────


def test_retry_receives_feedback_then_succeeds():
    attempts = []

    def flaky(note, sections, feedback):
        attempts.append(feedback)
        if feedback is None:  # first try: a verbatim violation
            return {"mentions": [{"type": "problem", "text": "wrong", "start": 0, "end": 5}]}
        return GOOD_RAW

    extraction = comprehend_text(NOTE, flaky)
    assert len(extraction.mentions) == 9
    assert attempts[0] is None and "verbatim" in attempts[1]


def test_exhausted_retries_fail_loudly():
    bad = stub_extractor({"mentions": [{"type": "problem", "text": "nope", "start": 0, "end": 4}]})
    with pytest.raises(ComprehensionError, match="after 3 attempts"):
        comprehend_text(NOTE, bad)


# ── registry entities ────────────────────────────────────────────────────────


def test_note_entities_registered(db_session):
    from hdh.core.models import Base

    tables = Base.metadata.tables
    assert "note_records" in tables and "note_mentions" in tables
    assert "concept_id" in tables["note_mentions"].c  # the ONLY ontology touch (§8)


def test_duplicate_relations_collapse_silently():
    raw = {
        "mentions": [_mention("medication", "Apixaban"), _mention("problem", "Essential hypertension")],
        "relations": [
            {"kind": "treats", "source": 0, "target": 1, "inferred": True},
            {"kind": "treats", "source": 0, "target": 1, "inferred": True},
        ],
    }
    extraction = build_extraction(NOTE, raw, segment(NOTE))
    assert len(extraction.relations) == 1  # noise, not an error — no retry burned


def test_control_attribute_legal_on_problems_only():
    raw = {
        "mentions": [
            _mention(
                "problem",
                "Essential hypertension",
                1,
                [{"kind": "control", "text": "Essential", "occurrence": 1}],
            )
        ]
    }
    extraction = build_extraction(NOTE, raw, segment(NOTE))
    assert extraction.mentions[0].attributes[0].kind is AttributeKind.CONTROL
    bad = {
        "mentions": [
            _mention("medication", "Apixaban", 1, [{"kind": "control", "text": "5mg", "occurrence": 1}])
        ]
    }
    assert any("illegal on a medication mention" in reason for reason in _reject(bad))
