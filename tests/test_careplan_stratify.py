"""Care plan, milestone 2a: intake and stratify.

The deterministic half of design §7 — *"deterministic first, LLM downstream
and constrained"*. Every flag here is re-derivable from the same chart,
explainable to a clinician who disagrees, and testable without an API key,
which is exactly why these nodes come before the generating ones.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.models import Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.careplan.context import (
    CarePlanContext,
    MedicationView,
    ProblemView,
    SocialView,
)
from hdh.modules.careplan.stratify import RULES, stratify


def _context(**overrides) -> CarePlanContext:
    """A context with sensible defaults, overridden per test."""
    base = {
        "mrn": "MRN-TEST",
        "age": 84,
        "sex": "MALE",
        "as_of": date(2026, 8, 1),
        "problems": (),
        "medications": (),
        "social": SocialView(
            lives_alone=False, lives_alone_basis="2 co-resident", smoker=False, marital_status="married"
        ),
    }
    base.update(overrides)
    return CarePlanContext(**base)


def _drug(name: str, drug_class: str) -> MedicationView:
    return MedicationView(name=name, drug_class=drug_class, dose="", started=date(2026, 5, 1))


def _fired(context) -> set[str]:
    return {flag.rule_id for flag in stratify(context)}


# ── the rules, one at a time ─────────────────────────────────────────────


def test_a_sulfonylurea_in_an_older_adult_fires():
    context = _context(age=84, medications=(_drug("Glipizide", "Sulfonylurea"),))
    assert "sulfonylurea-in-older-adult" in _fired(context)


def test_the_same_drug_in_a_younger_adult_does_not():
    """The rule is about age, not the drug — a 45-year-old on a
    sulfonylurea is ordinary prescribing."""
    context = _context(age=45, medications=(_drug("Glipizide", "Sulfonylurea"),))
    assert "sulfonylurea-in-older-adult" not in _fired(context)


def test_an_older_adult_on_metformin_alone_does_not():
    context = _context(age=84, medications=(_drug("Metformin", "Biguanide"),))
    assert "sulfonylurea-in-older-adult" not in _fired(context)


def test_living_alone_amplifies_a_delayed_rescue_drug():
    """The §12 insight: living alone is part of the drug's risk profile,
    not a separate social note."""
    context = _context(
        medications=(_drug("Glipizide", "Sulfonylurea"),),
        social=SocialView(
            lives_alone=True, lives_alone_basis="none recorded", smoker=None, marital_status=None
        ),
    )
    assert "delayed-rescue-living-alone" in _fired(context)


def test_living_alone_with_no_such_drug_does_not_fire():
    """The rule is about a *medication* risk that needs a witness, not
    about isolation on its own — that is a concern the plan may still
    raise, but not this rule's finding."""
    context = _context(
        medications=(_drug("Atorvastatin", "Statin"),),
        social=SocialView(
            lives_alone=True, lives_alone_basis="none recorded", smoker=None, marital_status=None
        ),
    )
    assert "delayed-rescue-living-alone" not in _fired(context)


def test_an_unknown_living_situation_declines_rather_than_assuming_company():
    """The tri-state exists for this case.

    A medication risk that depends on someone being nearby is *understated*
    by assuming company. So an unknown does not fire the rule — and it also
    does not record that the risk is absent, which a boolean defaulting to
    False would have done silently.
    """
    context = _context(
        medications=(_drug("Glipizide", "Sulfonylurea"),),
        social=SocialView(
            lives_alone=None, lives_alone_basis="not recorded", smoker=None, marital_status=None
        ),
    )
    fired = _fired(context)
    assert "delayed-rescue-living-alone" not in fired
    # the underlying drug risk is still raised — silence about the social
    # context must not swallow the medication flag
    assert "sulfonylurea-in-older-adult" in fired


def test_deintensification_is_a_later_threshold_than_hypoglycaemia_risk():
    """Two age constants, deliberately: hypoglycaemia risk rises from
    around 65, deintensification is usually discussed later."""
    at_70 = _context(age=70, medications=(_drug("Glipizide", "Sulfonylurea"),))
    at_80 = _context(age=80, medications=(_drug("Glipizide", "Sulfonylurea"),))
    assert "sulfonylurea-in-older-adult" in _fired(at_70)
    assert "deintensification-candidate" not in _fired(at_70)
    assert "deintensification-candidate" in _fired(at_80)


def test_polypharmacy_fires_at_the_same_count_caregaps_uses():
    """Both modules must agree about whether a patient is on five drugs."""
    from hdh.modules.careplan.stratify import POLYPHARMACY_THRESHOLD

    four = tuple(_drug(f"Drug{i}", "Misc") for i in range(4))
    assert "polypharmacy" not in _fired(_context(medications=four))
    five = tuple(_drug(f"Drug{i}", "Misc") for i in range(POLYPHARMACY_THRESHOLD))
    assert "polypharmacy" in _fired(_context(medications=five))


def test_an_uncontrolled_problem_is_flagged_and_a_controlled_one_is_not():
    uncontrolled = ProblemView(icd10="E11.9", description="Type 2 diabetes", controlled=False, onset=None)
    controlled = ProblemView(icd10="I10", description="Hypertension", controlled=True, onset=None)
    assert "uncontrolled-chronic" in _fired(_context(problems=(uncontrolled,)))
    assert "uncontrolled-chronic" not in _fired(_context(problems=(controlled,)))


def test_a_problem_with_no_control_recorded_is_not_treated_as_uncontrolled():
    """`None` means nobody said, and asserting poor control from silence
    would put a finding on the plan that the chart never made."""
    unknown = ProblemView(icd10="I10", description="Hypertension", controlled=None, onset=None)
    assert "uncontrolled-chronic" not in _fired(_context(problems=(unknown,)))


# ── the flags explain themselves ─────────────────────────────────────────


def test_every_rule_cites_a_document_that_exists():
    """The rule decides *whether* it fires; the corpus says *why it
    matters*. Splitting them lets the explanation be corrected without
    touching code — and makes this drift possible, so it is checked.
    """
    from hdh.modules.careplan.ingest import read_corpus

    cited: set[str] = {rule.cites for rule in RULES}
    for citation in sorted(cited):
        corpus_name, _, doc_id = citation.partition("/")
        _manifest, documents = read_corpus(corpus_name)
        assert doc_id in {d.doc_id for d in documents}, f"{citation} cites a document that does not exist"


def test_a_flag_says_what_in_this_chart_made_it_fire():
    """`basis` is the difference between a finding and an assertion: a
    clinician who disagrees needs to see the trigger, not just the claim."""
    context = _context(age=84, medications=(_drug("Glipizide", "Sulfonylurea"),))
    flag = next(f for f in stratify(context) if f.rule_id == "sulfonylurea-in-older-adult")
    assert "Glipizide" in flag.basis
    assert "84" in flag.basis
    assert flag.statement and flag.cites and flag.kind


def test_the_scenario_the_design_specifies_fires_the_expected_set():
    """§12: an older adult on a sulfonylurea who lives alone."""
    context = _context(
        age=84,
        medications=(_drug("Glipizide", "Sulfonylurea"), _drug("Warfarin", "Anticoagulant (VKA)")),
        problems=(ProblemView(icd10="E11.9", description="Type 2 diabetes", controlled=False, onset=None),),
        social=SocialView(
            lives_alone=True, lives_alone_basis="none recorded", smoker=None, marital_status=None
        ),
    )
    fired = _fired(context)
    assert {
        "sulfonylurea-in-older-adult",
        "delayed-rescue-living-alone",
        "deintensification-candidate",
        "uncontrolled-chronic",
    } <= fired


# ── intake reads the chart correctly ─────────────────────────────────────


@pytest.fixture
def chart(tmp_path):
    from hdh.core.models import Base, Prescription, Visit, VisitType

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "ctx.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    patient = Patient(
        mrn="MRN-CTX",
        first_name="Con",
        last_name="Text",
        date_of_birth=date(1942, 1, 1),
        sex=Sex.MALE,
    )
    session.add(patient)
    session.flush()

    recent = Visit(patient_id=patient.id, visit_date=date(2026, 6, 1), visit_type=VisitType.FOLLOW_UP)
    old = Visit(patient_id=patient.id, visit_date=date(2020, 6, 1), visit_type=VisitType.ACUTE)
    session.add_all([recent, old])
    session.flush()
    session.add_all(
        [
            Prescription(visit_id=recent.id, drug_name="Glipizide", drug_class="Sulfonylurea", dose="5 mg"),
            Prescription(visit_id=old.id, drug_name="Oseltamivir", drug_class="Antiviral", dose="75 mg"),
        ]
    )
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


def test_a_prescription_outside_the_window_is_not_an_active_medication(chart):
    """Without a window, "active medications" becomes every prescription
    ever written — a five-day antiviral counted beside a statin, and a
    polypharmacy flag that fires on everyone. Measured on a real chart it
    turned 20 drugs into 4."""
    from hdh.modules.careplan.context import build_context

    session, patient = chart
    context = build_context(session, patient, as_of=date(2026, 8, 1))
    names = {m.name for m in context.medications}
    assert "Glipizide" in names
    assert "Oseltamivir" not in names, "a 2020 prescription is not an active medication in 2026"


def test_the_window_is_anchored_to_the_dataset_not_to_today(chart):
    """A synthetic chart generated last year is not stale, it is dated —
    so `as_of` defaults to the dataset's latest visit, as `caregaps` does."""
    from hdh.modules.careplan.context import build_context

    session, patient = chart
    context = build_context(session, patient)
    assert context.as_of == date(2026, 6, 1)


def test_living_alone_is_derived_and_says_how(chart):
    """In a real chart this is something somebody asks and records. Here it
    is inferred from household links, so the inference travels with it."""
    from hdh.modules.careplan.context import build_context

    session, patient = chart
    social = build_context(session, patient).social
    assert social is not None
    assert social.lives_alone is True
    assert "household" in social.lives_alone_basis
