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
    assert {item.surface for item in vitals} >= {"BP", "HR", "Weight"}
    assert all(item.mention_type is MentionType.LAB_VITAL for item in vitals)
    assert all(item.expected_code for item in vitals), "a vital without a LOINC code can't score linking"

    # only the columns actually recorded — a null column renders nothing
    assert not any(item.surface == "BMI" for item in vitals)


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
