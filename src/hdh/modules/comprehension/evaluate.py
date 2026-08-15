"""The §8 evaluation harness: generated notes carry known ground truth.

The generator knows every code behind every synthetic note, so mention
recall, linking accuracy, and assertion accuracy cost zero annotation.
The scorer is pure (testable offline); the corpus loop drives the real
LLM extractor and is run locally by a keyed user, never in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hdh.modules.comprehension.contracts import Assertion, MentionType
from hdh.modules.comprehension.pipeline import ComprehendedNote


@dataclass(frozen=True)
class TruthItem:
    """One ground-truth entity the note is known to express."""

    surface: str  # the text the renderer emitted for it
    mention_type: MentionType
    expected_code: str | None = None  # snomed for problems, loinc for labs
    expected_assertion: Assertion | None = None


@dataclass
class Scorecard:
    """Aggregated counts; ratios computed at report time."""

    truth: int = 0
    found: int = 0
    extracted: int = 0
    linked_checked: int = 0
    linked_right: int = 0
    asserted_checked: int = 0
    asserted_right: int = 0
    misses: list[str] = field(default_factory=list)

    def add(self, other: Scorecard) -> None:
        self.truth += other.truth
        self.found += other.found
        self.extracted += other.extracted
        self.linked_checked += other.linked_checked
        self.linked_right += other.linked_right
        self.asserted_checked += other.asserted_checked
        self.asserted_right += other.asserted_right
        self.misses.extend(other.misses)

    def report(self) -> str:
        """The human-readable scoreboard (ratios computed here, never stored)."""
        recall = self.found / self.truth if self.truth else 0.0
        precision = self.found / self.extracted if self.extracted else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        linking = self.linked_right / self.linked_checked if self.linked_checked else 0.0
        assertion = self.asserted_right / self.asserted_checked if self.asserted_checked else 0.0
        lines = [
            f"mention recall    {recall:6.1%}   ({self.found}/{self.truth})",
            f"mention precision {precision:6.1%}   ({self.found}/{self.extracted} extracted)",
            f"mention F1        {f1:6.1%}",
            f"linking accuracy  {linking:6.1%}   ({self.linked_right}/{self.linked_checked} checked)",
            f"assertion accuracy{assertion:7.1%}   ({self.asserted_right}/{self.asserted_checked} checked)",
        ]
        if self.misses:
            lines.append("missed: " + "; ".join(self.misses[:8]))
        return "\n".join(lines)


def truth_for_visit(session, visit) -> tuple[TruthItem, ...]:
    """Ground truth the rendered note is KNOWN to contain (mirrors
    render_soap's inputs)."""
    patient = visit.patient
    items: list[TruthItem] = [
        TruthItem(a.substance, MentionType.ALLERGY, expected_assertion=Assertion.PRESENT)
        for a in patient.allergies
    ]
    for condition in patient.conditions:
        if condition.chronic and str(condition.status).endswith("ACTIVE"):
            items.append(
                TruthItem(
                    condition.description,
                    MentionType.PROBLEM,
                    expected_code=condition.snomed_code,
                    expected_assertion=Assertion.HISTORICAL,
                )
            )
    for history in patient.family_history:
        items.append(
            TruthItem(history.condition, MentionType.PROBLEM, expected_assertion=Assertion.FAMILY_HISTORY)
        )
    for condition in visit.conditions:
        items.append(
            TruthItem(
                condition.description,
                MentionType.PROBLEM,
                expected_code=condition.snomed_code,
                expected_assertion=Assertion.PRESENT,
            )
        )
    for rx in visit.prescriptions:
        items.append(TruthItem(rx.drug_name.split(" (")[0], MentionType.MEDICATION))
    for lab in visit.lab_results:
        if not str(lab.status).endswith("NORMAL"):  # only abnormal labs render
            items.append(TruthItem(lab.test_name, MentionType.LAB_VITAL, expected_code=lab.loinc_code))
    return tuple(items)


def score_note(truth: tuple[TruthItem, ...], note: ComprehendedNote) -> Scorecard:
    """Pure scorer: match ground truth against comprehended mentions."""
    card = Scorecard(truth=len(truth), extracted=len(note.mentions))
    for item in truth:
        surface = item.surface.lower()
        match = None
        for comprehended in note.mentions:
            if comprehended.mention.mention_type is not item.mention_type:
                continue
            text = comprehended.mention.text.lower()
            if surface in text or text in surface:
                match = comprehended
                break
        if match is None:
            card.misses.append(f"{item.mention_type.value}:{item.surface}")
            continue
        card.found += 1
        if item.expected_code:
            card.linked_checked += 1
            if match.code is not None and match.code.code == item.expected_code:
                card.linked_right += 1
        if item.expected_assertion:
            card.asserted_checked += 1
            if match.assertion.assertion is item.expected_assertion:
                card.asserted_right += 1
    return card


def evaluate_corpus(session, extractor, limit: int = 10) -> Scorecard:
    """Comprehend the first N stored visit notes and score them against
    ground truth — the honest-numbers loop (LLM cost: N extractions)."""
    from hdh.core.models import Visit, VisitNote
    from hdh.modules.comprehension.comprehend import ComprehensionError, comprehend_text
    from hdh.modules.comprehension.pipeline import comprehend_note

    total = Scorecard()
    notes = session.query(VisitNote).join(Visit, VisitNote.visit_id == Visit.id).limit(limit).all()
    for stored in notes:
        try:
            extraction = comprehend_text(stored.text, extractor)
        except ComprehensionError as err:
            total.misses.append(f"note {stored.id}: FAILED ({err})")
            continue
        comprehended = comprehend_note(session, extraction)
        total.add(score_note(truth_for_visit(session, stored.visit), comprehended))
    return total
