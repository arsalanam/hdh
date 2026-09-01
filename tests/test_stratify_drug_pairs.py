"""Rules that look at a PAIR of drugs (#102).

Every rule `stratify` had fired on a single drug or a count. None looked at
two together, so a chart with an NSAID beside an anticoagulant produced
"polypharmacy — regimen complexity worth reviewing": technically true and
completely silent about the actual danger in the list.

The grader found it and the rules could not, which is the right prompt to
extend the rules rather than to lean on the model — a flag from a rule can
be re-derived tomorrow and argued with.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.modules.careplan.context import CarePlanContext, MedicationView, ProblemView
from hdh.modules.careplan.stratify import DUPLICATE_WATCH, RULES, _class_family, stratify


def _med(name: str, drug_class: str) -> MedicationView:
    return MedicationView(name=name, drug_class=drug_class, dose="", started=date(2026, 1, 1))


def _context(*, meds=(), problems=(), age: int = 70) -> CarePlanContext:
    return CarePlanContext(
        mrn="MRN-TEST",
        age=age,
        sex="FEMALE",
        problems=tuple(problems),
        medications=tuple(meds),
    )


def _fired(context) -> dict[str, str]:
    return {flag.rule_id: flag.basis for flag in stratify(context)}


# ── the family normaliser, which is what made this invisible ─────────────


@pytest.mark.parametrize(
    ("drug_class", "family"),
    [
        ("NSAID", "nsaid"),
        ("COX-2 NSAID", "nsaid"),
        ("Statin", "statin"),
        ("Statin (high-intensity)", "statin"),
        ("Anticoagulant (DOAC)", "anticoagulant"),
        ("Anticoagulant (VKA)", "anticoagulant"),
    ],
)
def test_a_family_is_recognised_however_it_is_spelled(drug_class, family):
    """The formulary spells one family several ways. Comparing the strings
    exactly is how two NSAIDs got past both the generator's guard and every
    rule here."""
    assert _class_family(drug_class) == family


def test_unrelated_classes_do_not_collapse_into_one_family():
    assert _class_family("Beta blocker") != _class_family("Calcium channel blocker")


# ── NSAID with an anticoagulant ──────────────────────────────────────────


def test_an_nsaid_beside_an_anticoagulant_fires():
    context = _context(meds=[_med("Ibuprofen", "NSAID"), _med("Warfarin", "Anticoagulant (VKA)")])
    assert "nsaid-with-anticoagulant" in _fired(context)


def test_the_flag_names_both_drugs():
    """ "A drug interaction was found" is not something a clinician can act
    on or disagree with."""
    context = _context(meds=[_med("Ibuprofen", "NSAID"), _med("Apixaban", "Anticoagulant (DOAC)")])
    basis = _fired(context)["nsaid-with-anticoagulant"]
    assert "Ibuprofen" in basis and "Apixaban" in basis


def test_a_cox2_nsaid_counts_as_an_nsaid_for_bleeding_risk():
    context = _context(meds=[_med("Meloxicam", "COX-2 NSAID"), _med("Warfarin", "Anticoagulant (VKA)")])
    assert "nsaid-with-anticoagulant" in _fired(context)


def test_either_drug_alone_does_not_fire():
    """Neither agent is contraindicated on its own — that is the whole
    reason a rule reading one drug at a time found nothing."""
    assert "nsaid-with-anticoagulant" not in _fired(_context(meds=[_med("Ibuprofen", "NSAID")]))
    assert "nsaid-with-anticoagulant" not in _fired(_context(meds=[_med("Warfarin", "Anticoagulant (VKA)")]))


# ── duplicate therapy within a class ─────────────────────────────────────


def test_two_nsaids_fire_even_when_classed_differently():
    """The case from the issue: meloxicam and ibuprofen are one family
    spelled two ways."""
    context = _context(meds=[_med("Meloxicam", "COX-2 NSAID"), _med("Ibuprofen", "NSAID")])
    basis = _fired(context)["duplicate-class-therapy"]
    assert "Meloxicam" in basis and "Ibuprofen" in basis


def test_two_statins_fire():
    context = _context(meds=[_med("Atorvastatin", "Statin (high-intensity)"), _med("Simvastatin", "Statin")])
    assert "duplicate-class-therapy" in _fired(context)


def test_the_same_drug_twice_is_not_duplicate_therapy():
    """A repeat of one drug is a medication list artefact, not two agents."""
    context = _context(meds=[_med("Ibuprofen", "NSAID"), _med("Ibuprofen", "NSAID")])
    assert "duplicate-class-therapy" not in _fired(context)


def test_dual_antiplatelet_therapy_is_not_flagged():
    """Standard after a stent. Flagging it would train a reader to ignore
    the flag, which costs more than the rule is worth."""
    context = _context(meds=[_med("Aspirin", "Antiplatelet"), _med("Clopidogrel", "Antiplatelet")])
    assert "duplicate-class-therapy" not in _fired(context)


def test_two_antibiotics_are_not_flagged():
    """Courses follow one another; that is not duplication."""
    context = _context(
        meds=[_med("Amoxicillin", "Penicillin antibiotic"), _med("Azithromycin", "Macrolide antibiotic")]
    )
    assert "duplicate-class-therapy" not in _fired(context)


def test_the_watch_list_is_explicit_rather_than_any_repeated_class():
    """Deliberately a list. "Any repeated class" would flag antibiotics and
    dual antiplatelet therapy."""
    assert "antiplatelet" not in DUPLICATE_WATCH
    assert "antibiotic" not in DUPLICATE_WATCH
    assert "nsaid" in DUPLICATE_WATCH


# ── NSAID where the kidneys or heart cannot absorb it ────────────────────


@pytest.mark.parametrize("icd10", ["N18.31", "N18.4", "N18.5", "I50.32"])
def test_an_nsaid_with_impairment_fires(icd10):
    context = _context(
        meds=[_med("Meloxicam", "COX-2 NSAID")],
        problems=[ProblemView(icd10, "A problem", None, None)],
    )
    assert "nsaid-in-renal-or-cardiac-impairment" in _fired(context)


@pytest.mark.parametrize("icd10", ["N18.1", "N18.2"])
def test_early_ckd_does_not_fire(icd10):
    """The concern is meaningful impairment, not any CKD code at all."""
    context = _context(
        meds=[_med("Meloxicam", "COX-2 NSAID")],
        problems=[ProblemView(icd10, "Early CKD", None, None)],
    )
    assert "nsaid-in-renal-or-cardiac-impairment" not in _fired(context)


def test_the_flag_names_the_drug_and_the_problem():
    context = _context(
        meds=[_med("Meloxicam", "COX-2 NSAID")],
        problems=[ProblemView("I50.32", "Chronic diastolic heart failure", None, None)],
    )
    basis = _fired(context)["nsaid-in-renal-or-cardiac-impairment"]
    assert "Meloxicam" in basis and "I50.32" in basis


# ── each rule cites prose that exists ────────────────────────────────────


def test_every_rule_cites_a_document_on_disk():
    """A flag citing a document that is not there is an unsupported flag,
    and traceability is the dimension that governs the grade."""
    import pathlib

    root = pathlib.Path("src/hdh/modules/careplan/knowledge")
    for rule in RULES:
        corpus, _, doc_id = rule.cites.partition("/")
        assert (root / corpus / f"{doc_id}.md").is_file(), f"{rule.rule_id} cites a missing document"


def test_the_three_new_rules_are_registered():
    ids = {rule.rule_id for rule in RULES}
    assert {
        "nsaid-with-anticoagulant",
        "duplicate-class-therapy",
        "nsaid-in-renal-or-cardiac-impairment",
    } <= ids


def test_a_chart_with_no_hazard_raises_none_of_them():
    """The flags have to be absent when the danger is absent, or they say
    nothing when present."""
    context = _context(meds=[_med("Levothyroxine", "Thyroid hormone replacement")])
    fired = _fired(context)
    for rule_id in (
        "nsaid-with-anticoagulant",
        "duplicate-class-therapy",
        "nsaid-in-renal-or-cardiac-impairment",
    ):
        assert rule_id not in fired
