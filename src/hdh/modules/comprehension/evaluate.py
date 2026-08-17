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
    slice_name: str = "other"  # which part of the note it came from (§14.3)


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
    by_slice: dict[str, list[int]] = field(default_factory=dict)  # name -> [found, truth]

    def add(self, other: Scorecard) -> None:
        """Fold another note's counts in — totals and per-slice alike."""
        self.truth += other.truth
        self.found += other.found
        self.extracted += other.extracted
        self.linked_checked += other.linked_checked
        self.linked_right += other.linked_right
        self.asserted_checked += other.asserted_checked
        self.asserted_right += other.asserted_right
        self.misses.extend(other.misses)
        for name, (found, truth) in other.by_slice.items():
            totals = self.by_slice.setdefault(name, [0, 0])
            totals[0] += found
            totals[1] += truth

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
        for name in sorted(self.by_slice):
            found, truth = self.by_slice[name]
            share = found / truth if truth else 0.0
            lines.append(f"  recall · {name:<16}{share:6.1%}   ({found}/{truth})")
        if self.misses:
            lines.append("missed: " + "; ".join(self.misses[:8]))
        return "\n".join(lines)


def truth_for_visit(session, visit) -> tuple[TruthItem, ...]:
    """Ground truth the rendered note is KNOWN to contain (mirrors
    render_soap's inputs)."""
    patient = visit.patient
    items: list[TruthItem] = [
        TruthItem(
            a.substance, MentionType.ALLERGY, expected_assertion=Assertion.PRESENT, slice_name="allergy"
        )
        for a in patient.allergies
    ]
    for condition in patient.conditions:
        if condition.chronic and str(condition.status).endswith("ACTIVE"):
            if not _recorded_by(condition, visit):
                continue  # not yet diagnosed when this note was written
            items.append(
                TruthItem(
                    condition.description,
                    MentionType.PROBLEM,
                    expected_code=condition.snomed_code,
                    expected_assertion=Assertion.HISTORICAL,
                    slice_name="history-line",
                )
            )
    for history in patient.family_history:
        items.append(
            TruthItem(
                history.condition,
                MentionType.PROBLEM,
                expected_assertion=Assertion.FAMILY_HISTORY,
                slice_name="family-history",
            )
        )
    for condition in visit.conditions:
        items.append(
            TruthItem(
                condition.description,
                MentionType.PROBLEM,
                expected_code=condition.snomed_code,
                expected_assertion=Assertion.PRESENT,
                slice_name="assessment",
            )
        )
    for rx in visit.prescriptions:
        items.append(TruthItem(rx.drug_name.split(" (")[0], MentionType.MEDICATION, slice_name="medication"))
    for lab in visit.lab_results:
        if not str(lab.status).endswith("NORMAL"):  # only abnormal labs render
            items.append(
                TruthItem(
                    lab.test_name, MentionType.LAB_VITAL, expected_code=lab.loinc_code, slice_name="lab"
                )
            )
    items.extend(_vital_truth(visit))
    return tuple(items)


#: What render_soap prints in the objective section, and the LOINC code
#: the normalizer should reach for it. Ground truth omitted these until
#: §14.3 — every correctly-extracted vital counted as a false positive,
#: which is exactly why the §12 precision figure reads low.
VITAL_TRUTH: tuple[tuple[str, str, str], ...] = (
    ("bp_systolic", "BP", "55284-4"),
    ("heart_rate", "HR", "8867-4"),
    ("respiratory_rate", "RR", "9279-1"),
    ("temperature_f", "Temp", "8310-5"),
    ("oxygen_sat", "O2 sat", "59408-5"),
    ("weight_kg", "Weight", "29463-7"),
    ("bmi", "BMI", "39156-5"),
)


def _vital_truth(visit) -> list[TruthItem]:
    """The vitals panel the note actually rendered (design §14.3)."""
    vitals = getattr(visit, "vitals", None)
    if vitals is None:
        return []
    return [
        TruthItem(surface, MentionType.LAB_VITAL, expected_code=loinc, slice_name="vitals")
        for column, surface, loinc in VITAL_TRUTH
        if getattr(vitals, column, None) is not None
    ]


def _recorded_by(condition, visit) -> bool:
    """Was this chronic condition already on the chart when the note was
    written? (design §14.3)

    `render_soap`'s "History of:" line lists the conditions diagnosed *so
    far* — the generator accumulates them visit by visit. Ground truth
    built from the patient's problem list as it stands TODAY asks a 2024
    note to mention a 2026 diagnosis, which shows up as a recall miss the
    extractor could never have avoided. Same defect the missing vitals
    had: truth claiming what the note does not say."""
    recorded = getattr(condition, "visit", None)
    if recorded is not None and recorded.visit_date is not None:
        return recorded.visit_date <= visit.visit_date
    if condition.onset_date is not None:
        return condition.onset_date <= visit.visit_date
    return True  # undateable: keep it rather than silently shrink the truth


def score_note(truth: tuple[TruthItem, ...], note: ComprehendedNote) -> Scorecard:
    """Pure scorer: match ground truth against comprehended mentions."""
    card = Scorecard(truth=len(truth), extracted=len(note.mentions))
    for item in truth:
        card.by_slice.setdefault(item.slice_name, [0, 0])[1] += 1
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
        card.by_slice[item.slice_name][0] += 1
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
