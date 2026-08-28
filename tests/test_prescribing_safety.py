"""The generator does not prescribe a second drug in a class already running.

Nobody is on two statins. Before this check, 27 of 178 generated patients
were on a duplicated class — sixteen on two statins, five on two NSAIDs, one
on two SSRIs — because each condition picked its drugs without ever looking
at what the patient was already taking.

That mattered beyond realism: the care-plan grader twice scored the pipeline
well for catching hazards **our own generator invented**, which flatters it
and says nothing about real charts.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.generators import (
    CONCURRENT_CLASSES_OK,
    _class_root,
    _current_medications,
    _prescription_is_current,
    _would_duplicate_a_class,
)


class Spec:
    def __init__(self, name: str, klass: str) -> None:
        self.drug_name = name
        self.drug_class = klass


def _rx(name: str, klass: str, days: int | None = None) -> dict:
    return {"drug_name": name, "drug_class": klass, "duration_days": days}


TODAY = date(2026, 1, 1)


# ── the class root ───────────────────────────────────────────────────────


def test_a_qualified_class_matches_its_root():
    """The bug that let two statins through even with the check in place.

    The formulary spells one class two ways: `hyperlipidemia` prescribes
    Atorvastatin as `Statin`, `cad` prescribes it as
    `Statin (high-intensity)`. Both are true and the distinction is worth
    keeping — but comparing the strings exactly makes them different
    classes, and nothing recognised the second statin as a statin.
    """
    assert _class_root("Statin (high-intensity)") == "statin"
    assert _class_root("Statin") == "statin"
    assert _class_root(None) == ""


def test_the_qualifier_survives_in_the_record():
    """Normalisation is for the comparison, not for the data. A
    high-intensity statin is a real clinical distinction."""
    spec = Spec("Atorvastatin", "Statin (high-intensity)")
    assert spec.drug_class == "Statin (high-intensity)"


# ── what counts as running ───────────────────────────────────────────────


def test_a_course_with_a_duration_ends_when_it_ends():
    """A five-day antibiotic is not a current medication in month eleven,
    and treating it as one is how polypharmacy comes to include things the
    patient finished last spring."""
    started = date(2025, 12, 1)
    assert _prescription_is_current(started, _rx("Amoxicillin", "Antibiotic", 7), date(2025, 12, 5))
    assert not _prescription_is_current(started, _rx("Amoxicillin", "Antibiotic", 7), TODAY)


def test_ongoing_therapy_falls_back_to_the_window():
    assert _prescription_is_current(date(2025, 6, 1), _rx("Atorvastatin", "Statin"), TODAY)
    assert not _prescription_is_current(date(2023, 6, 1), _rx("Atorvastatin", "Statin"), TODAY)


def test_current_medications_reports_roots_and_names():
    stream = [(date(2025, 12, 1), _rx("Atorvastatin", "Statin (high-intensity)"))]
    classes, names = _current_medications(stream, TODAY)
    assert classes == {"statin"}
    assert names == {"atorvastatin"}


# ── the rule ─────────────────────────────────────────────────────────────


def test_a_second_statin_is_refused():
    stream = [(date(2025, 12, 1), _rx("Simvastatin", "Statin"))]
    assert _would_duplicate_a_class(Spec("Atorvastatin", "Statin"), stream, TODAY)


def test_a_second_statin_is_refused_across_the_two_spellings():
    stream = [(date(2025, 12, 1), _rx("Simvastatin", "Statin"))]
    assert _would_duplicate_a_class(Spec("Atorvastatin", "Statin (high-intensity)"), stream, TODAY)


def test_a_repeat_of_the_same_drug_is_not_a_duplicate():
    """Renewing a statin is the commonest event in primary care. Blocking it
    would leave a chart showing one prescription years ago and nothing
    since, which reads as having stopped."""
    stream = [(date(2025, 12, 1), _rx("Atorvastatin", "Statin"))]
    assert not _would_duplicate_a_class(Spec("Atorvastatin", "Statin"), stream, TODAY)


def test_a_different_class_is_not_a_duplicate():
    """Two antihypertensives of different classes is normal prescribing —
    the rule is per class, so it never had to be told that."""
    stream = [(date(2025, 12, 1), _rx("Lisinopril", "ACE inhibitor"))]
    assert not _would_duplicate_a_class(Spec("Metoprolol", "Beta blocker"), stream, TODAY)


def test_dual_antiplatelet_therapy_is_allowed():
    """The one real exception. Aspirin with clopidogrel is standard after a
    stent or an acute coronary syndrome, so the check must not intervene."""
    assert "antiplatelet" in CONCURRENT_CLASSES_OK
    stream = [(date(2025, 12, 1), _rx("Aspirin", "Antiplatelet"))]
    assert not _would_duplicate_a_class(Spec("Clopidogrel", "Antiplatelet"), stream, TODAY)


def test_a_finished_course_does_not_block_the_next_one():
    """Sequential antibiotic courses are normal. The first has ended, so it
    is not a concurrent duplicate."""
    stream = [(date(2025, 1, 1), _rx("Nitrofurantoin", "Antibiotic", 7))]
    assert not _would_duplicate_a_class(Spec("Trimethoprim-SMX", "Antibiotic"), stream, TODAY)


def test_a_drug_with_no_class_is_never_blocked():
    """An unclassed drug cannot be shown to duplicate anything, and refusing
    it on a guess would remove real prescriptions."""
    stream = [(date(2025, 12, 1), _rx("Something", "Statin"))]
    assert not _would_duplicate_a_class(Spec("Mystery", ""), stream, TODAY)


# ── the property, end to end ─────────────────────────────────────────────


@pytest.mark.parametrize("seed", [4242, 7])
def test_no_generated_patient_is_on_two_drugs_of_one_chronic_class(tmp_path, seed):
    """The measured claim. Statins went from sixteen affected patients to
    none.

    Acute classes are excluded because the residue there is a *different*
    defect: `build_context` treats a finished seven-day course as an active
    medication for a year, so sequential NSAID courses still read as
    concurrent to anything downstream. Filed separately.
    """
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.context import build_context

    bootstrap_schema()
    engine = get_engine(str(tmp_path / f"gen-{seed}.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    try:
        build_dataset(session, n_patients=40, years_of_history=4, verbose=False, seed=seed)
        acute = {"antibiotic", "nsaid", "topical steroid", "antiplatelet"}
        offenders = []
        for patient in session.query(Patient).all():
            context = build_context(session, patient)
            by_class: dict[str, list[str]] = {}
            for medication in context.medications:
                root = _class_root(medication.drug_class)
                if root and root not in acute:
                    by_class.setdefault(root, []).append(medication.name)
            offenders += [
                f"{context.mrn}: {root} = {', '.join(names)}"
                for root, names in by_class.items()
                if len(names) > 1
            ]
        assert not offenders, "concurrent duplicate-class prescribing: " + "; ".join(offenders)
    finally:
        session.close()
        engine.dispose()
