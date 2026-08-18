"""The scorer's own correctness (design §14.3).

The §12 baseline came with three caveats. Two were scorer defects, not
pipeline defects: vitals were missing from ground truth (so every
correctly-extracted vital counted against precision), and history-line
recall was invisible inside the aggregate. Both are fixed — these tests
pin the fixes so the numbers can't quietly drift back.

Pure and offline: the scorer takes ground truth and a comprehended note,
and never calls anything.
"""

from datetime import date

import pytest

from hdh.core.models import Patient, Sex, Visit, VisitType, Vital, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.contracts import Assertion, MentionType
from hdh.modules.comprehension.evaluate import (
    VITAL_TRUTH,
    Scorecard,
    TruthItem,
    _best_match,
    score_note,
    truth_for_visit,
)


@pytest.fixture()
def visit_with_vitals(tmp_path):
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "scoring.db"))
    session = get_session(engine)
    patient = Patient(
        mrn="MRN00SCORER",
        first_name="Score",
        last_name="Card",
        date_of_birth=date(1968, 8, 8),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 6, 6), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    session.add(Vital(visit_id=visit.id, bp_systolic=142, bp_diastolic=88, heart_rate=76, weight_kg=82.0))
    session.commit()
    yield session, visit
    session.close()
    engine.dispose()


def test_ground_truth_now_includes_the_vitals_the_note_renders(visit_with_vitals):
    """The §12 precision caveat, closed: a rendered vital is ground truth,
    so extracting it is a hit rather than an unmatched extraction."""
    session, visit = visit_with_vitals
    truth = truth_for_visit(session, visit)
    vitals = [item for item in truth if item.slice_name == "vitals"]
    surfaces = {item.surface for item in vitals}
    assert surfaces >= {"BP", "HR"}
    assert all(item.mention_type is MentionType.LAB_VITAL for item in vitals)
    assert all(item.expected_code for item in vitals), "a vital without a LOINC code can't score linking"

    # only the columns actually recorded — a null column renders nothing
    assert not any(item.surface == "BMI" for item in vitals)

    # and NOTHING the note never prints: render_soap emits no weight or
    # height, so claiming them would be an unavoidable recall miss
    assert "Weight" not in surfaces and "Height" not in surfaces


def test_vital_truth_table_is_wellformed():
    columns = [column for column, _, _ in VITAL_TRUTH]
    codes = [loinc for _, _, loinc in VITAL_TRUTH]
    assert len(columns) == len(set(columns)) and len(codes) == len(set(codes))
    assert all(code and code[0].isdigit() for code in codes), "LOINC codes are numeric-leading"


class _FakeMention:
    def __init__(self, text, mention_type):
        self.text, self.mention_type, self.attributes = text, mention_type, ()


class _FakeComprehended:
    def __init__(self, text, mention_type, code=None, assertion=Assertion.PRESENT):
        self.mention = _FakeMention(text, mention_type)
        self.code = code
        self.assertion = type("A", (), {"assertion": assertion})()


class _FakeNote:
    def __init__(self, mentions):
        self.mentions = mentions


def test_per_slice_recall_is_reported_separately():
    """History-line recall was the §12 blind spot — it is now its own
    number, which is what makes it a tuning target."""
    truth = (
        TruthItem("Essential hypertension", MentionType.PROBLEM, slice_name="history-line"),
        TruthItem("Chronic kidney disease", MentionType.PROBLEM, slice_name="history-line"),
        TruthItem("Atrial fibrillation", MentionType.PROBLEM, slice_name="assessment"),
    )
    note = _FakeNote(
        [
            _FakeComprehended("Essential hypertension", MentionType.PROBLEM),
            _FakeComprehended("Atrial fibrillation", MentionType.PROBLEM),
        ]
    )
    card = score_note(truth, note)
    assert card.by_slice["history-line"] == [1, 2]  # one of two found
    assert card.by_slice["assessment"] == [1, 1]

    report = card.report()
    assert "recall · history-line" in report and "50.0%" in report
    assert "recall · assessment" in report


def test_slices_aggregate_across_notes():
    first, second = Scorecard(), Scorecard()
    first.by_slice = {"history-line": [1, 2]}
    second.by_slice = {"history-line": [2, 3], "vitals": [4, 4]}
    first.add(second)
    assert first.by_slice["history-line"] == [3, 5]
    assert first.by_slice["vitals"] == [4, 4]


def test_extracting_a_vital_no_longer_costs_precision(visit_with_vitals):
    """The regression the fix exists to prevent, stated as an assertion:
    a note whose every extraction is correct scores 100% precision."""
    session, visit = visit_with_vitals
    truth = truth_for_visit(session, visit)
    note = _FakeNote(
        [
            _FakeComprehended(
                item.surface, item.mention_type, assertion=item.expected_assertion or Assertion.PRESENT
            )
            for item in truth
        ]
    )
    card = score_note(truth, note)
    assert card.found == card.truth == card.extracted
    assert "100.0%" in card.report()


def test_history_truth_excludes_conditions_diagnosed_after_the_note(tmp_path):
    """A note cannot mention a diagnosis that did not exist yet.

    The generator's "History of:" line accumulates visit by visit, so
    ground truth built from today's problem list penalised old notes for
    future diagnoses — 49.4% history-line recall in the first N=25 run was
    mostly this, not the extractor.
    """
    from hdh.core.models import Condition, ConditionStatus

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "dated.db"))
    session = get_session(engine)
    patient = Patient(
        mrn="MRN00DATED1",
        first_name="Date",
        last_name="Filter",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.MALE,
    )
    session.add(patient)
    session.flush()
    early = Visit(patient_id=patient.id, visit_date=date(2024, 3, 1), visit_type=VisitType.FOLLOW_UP)
    late = Visit(patient_id=patient.id, visit_date=date(2026, 3, 1), visit_type=VisitType.FOLLOW_UP)
    session.add_all([early, late])
    session.flush()
    session.add_all(
        [
            Condition(
                patient_id=patient.id,
                visit_id=early.id,
                icd10_code="E03.9",
                description="Hypothyroidism, unspecified",
                chronic=True,
                status=ConditionStatus.ACTIVE,
                onset_date=date(2024, 3, 1),
            ),
            Condition(
                patient_id=patient.id,
                visit_id=late.id,
                icd10_code="I10",
                description="Essential hypertension",
                chronic=True,
                status=ConditionStatus.ACTIVE,
                onset_date=date(2026, 3, 1),
            ),
        ]
    )
    session.commit()

    early_history = {i.surface for i in truth_for_visit(session, early) if i.slice_name == "history-line"}
    late_history = {i.surface for i in truth_for_visit(session, late) if i.slice_name == "history-line"}
    assert early_history == {"Hypothyroidism, unspecified"}, early_history
    assert late_history == {"Hypothyroidism, unspecified", "Essential hypertension"}

    session.close()
    engine.dispose()


def test_vital_truth_mirrors_what_the_note_actually_renders():
    """Every surface must be a token `render_soap` prints, and every
    printed vital must be claimed — the two-sided property that the first
    version of this table got wrong in both directions."""
    from hdh.core.notes import render_soap

    class _V:
        bp_systolic, bp_diastolic, heart_rate = 138, 82, 72
        respiratory_rate, temperature_f, oxygen_sat = 14, 98.4, 96
        weight_kg, height_cm, bmi, pain_scale = 82.0, 170.0, 32.0, 2

    note = render_soap(
        provider_name="Dr. Test",
        visit_date="2026-06-06",
        chief_complaint="Follow-up",
        follow_up_days=90,
        age=64,
        sex="female",
        allergies=[],
        chronic_history=[],
        family_history=[],
        vital=_V(),
        conditions=[],
        prescriptions=[],
        labs=[],
        procedures=[],
    )
    vitals_line = next(line for line in note.splitlines() if "Vitals:" in line)
    for _column, surface, _loinc in VITAL_TRUTH:
        assert surface in vitals_line, f"{surface!r} is claimed as truth but never rendered"
    # and the reverse: every token before a value in the line is claimed
    for token in ("BP", "HR", "RR", "T", "SpO2", "BMI", "pain"):
        assert any(surface == token for _c, surface, _l in VITAL_TRUTH), (
            f"{token!r} is rendered but unclaimed"
        )


def test_short_mentions_must_match_exactly():
    """The bug that cost ~50 phantom linking misses: "T" is a substring of
    "Weight", "Temp" and "O2 sat", so a loose containment rule paired it
    with whichever truth item came first and then scored its LOINC wrong."""
    truth_weight = TruthItem("Weight", MentionType.LAB_VITAL, expected_code="29463-7", slice_name="vitals")
    truth_temp = TruthItem("T", MentionType.LAB_VITAL, expected_code="8310-5", slice_name="vitals")
    note = _FakeNote([_FakeComprehended("T", MentionType.LAB_VITAL)])

    assert _best_match(truth_weight, note.mentions) is None, "'T' must not satisfy 'Weight'"
    assert _best_match(truth_temp, note.mentions) is not None, "'T' must still match 'T' exactly"


def test_containment_still_matches_real_variants():
    """The fix must not over-correct: genuine partial matches still count."""
    truth = TruthItem("Hypothyroidism, unspecified", MentionType.PROBLEM, slice_name="history-line")
    note = _FakeNote(
        [
            _FakeComprehended("Hyperlipidemia", MentionType.PROBLEM),
            _FakeComprehended("Hypothyroidism", MentionType.PROBLEM),
        ]
    )
    match = _best_match(truth, note.mentions)
    assert match is not None and match.mention.text == "Hypothyroidism"


def test_the_longest_candidate_wins_not_the_first():
    truth = TruthItem("Chronic kidney disease, stage 3a", MentionType.PROBLEM, slice_name="assessment")
    note = _FakeNote(
        [
            _FakeComprehended("disease", MentionType.PROBLEM),
            _FakeComprehended("Chronic kidney disease", MentionType.PROBLEM),
        ]
    )
    assert _best_match(truth, note.mentions).mention.text == "Chronic kidney disease"


def test_the_same_condition_in_two_sections_scores_each_separately():
    """A chronic problem appears in the history line AND the assessment
    with different expected assertions (historical vs present). Matching
    on text alone paired both truth items with the same mention, so one
    was scored wrong no matter what the pipeline did — 9 guaranteed
    misses across 25 notes."""
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.extract import stub_extractor

    note = (
        "SOAP NOTE\nProvider: Dr. Test\n\n"
        "S: Reports fatigue. History of: Hypothyroidism.\n\n"
        "O: BP 128/78 mmHg.\n\n"
        "A: Hypothyroidism.\n\n"
        "P: Continue Levothyroxine 50mcg.\n"
    )
    raw = {
        "mentions": [
            {"type": "problem", "text": "Hypothyroidism", "occurrence": 1, "attributes": []},
            {"type": "problem", "text": "Hypothyroidism", "occurrence": 2, "attributes": []},
        ]
    }
    extraction = comprehend_text(note, stub_extractor(raw))
    assert len(extraction.mentions) == 2, "the two occurrences must survive as separate mentions"

    class _Item:
        def __init__(self, comprehended):
            self.mention = comprehended

    kinds = {extraction.section_of(m).kind.value for m in extraction.mentions}
    assert kinds == {"subjective_history", "assessment"}

    history_truth = TruthItem(
        "Hypothyroidism",
        MentionType.PROBLEM,
        expected_assertion=Assertion.HISTORICAL,
        slice_name="history-line",
    )
    assessment_truth = TruthItem(
        "Hypothyroidism",
        MentionType.PROBLEM,
        expected_assertion=Assertion.PRESENT,
        slice_name="assessment",
    )

    class _Note:
        def __init__(self, extraction):
            self.extraction = extraction
            self.mentions = [_Item(m) for m in extraction.mentions]

    note_obj = _Note(extraction)
    from_history = _best_match(history_truth, note_obj.mentions, note_obj)
    from_assessment = _best_match(assessment_truth, note_obj.mentions, note_obj)
    assert from_history is not None and from_assessment is not None
    assert from_history is not from_assessment, "each truth item must find ITS OWN mention"
    assert extraction.section_of(from_history.mention).kind.value == "subjective_history"
    assert extraction.section_of(from_assessment.mention).kind.value == "assessment"


def test_section_narrowing_never_hides_a_mention():
    """If the extractor put the mention in a different section than
    expected, it must still be matched and scored — narrowing is a
    preference, not a filter, or a placement error would masquerade as a
    recall miss."""
    truth = TruthItem("Hypothyroidism", MentionType.PROBLEM, slice_name="history-line")
    note = _FakeNote([_FakeComprehended("Hypothyroidism", MentionType.PROBLEM)])
    assert _best_match(truth, note.mentions, note) is not None
