"""The §10 scenarios: notes we did not write.

Every note in the corpus before these was written by us, and it showed —
SOAP headers, one drug per sentence, a strength spelled the way the
catalog spells it. These are compressed, unstructured and full of things
mentioned without being ordered, which is what real notes are like.

Scope, as §10 sets it: RxNorm's milestones assert the MEDICATION rows.
The rest are recorded so the modules that own them are measured against
the same notes rather than against convenient ones, and rows that cannot
be satisfied yet are marked as expected failures WITH the reason — the
way the #53 frontier list is — so the corpus says what is not working
instead of quietly not asking.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.ontology import get_ontology_service
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.comprehend import comprehend_text
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.rxnorm.coding import parse_strength, resolve

RX_FIXTURE = Path(__file__).parent / "fixtures" / "rxnorm"
sys.path.insert(0, str(RX_FIXTURE))
import rxnorm_ids as rx  # noqa: E402

# ── Scenario A, in the fixture's vocabulary ──────────────────────────────
#
# The real note names Metformin ER and Junovia. The fabricated release has
# Blorbizide (with an extended-release product) and Zorbex, so the note is
# rewritten onto those names while every DIFFICULTY is kept: a generic
# with a release form, a quantity-times-strength, a brand, a lab result in
# prose, a lab order, two exams, and no vitals anywhere.

SCENARIO_A = (
    "patient with h/o type 2 diabetes and well treated hypertension came with "
    "higher than 7 Hba1c. i continued Blorbizide ER 2 x 10mg with evening meal "
    "and added Zorbex 10 mg OD and asked for repeat HbA1c after 90 days. "
    "eyesight and foot exam was normal. refill and new drug order placed"
)

SCENARIO_A_RAW = {
    "mentions": [
        {"type": "problem", "text": "type 2 diabetes", "occurrence": 1, "attributes": []},
        {"type": "problem", "text": "hypertension", "occurrence": 1, "attributes": []},
        {
            "type": "lab_vital",
            "text": "Hba1c",
            "occurrence": 1,
            "attributes": [{"kind": "value", "text": "7", "occurrence": 1}],
        },
        {
            "type": "medication",
            "text": "Blorbizide",
            "occurrence": 1,
            "attributes": [
                {"kind": "dose", "text": "10mg", "occurrence": 1},
                {"kind": "frequency", "text": "with evening meal", "occurrence": 1},
                {"kind": "status_word", "text": "continued", "occurrence": 1},
            ],
        },
        {
            "type": "medication",
            "text": "Zorbex",
            "occurrence": 1,
            "attributes": [
                {"kind": "dose", "text": "10 mg", "occurrence": 1},
                {"kind": "frequency", "text": "OD", "occurrence": 1},
                {"kind": "status_word", "text": "added", "occurrence": 1},
            ],
        },
        {
            # the note spells the test two ways — "Hba1c" for the result it
            # came in with, "HbA1c" for the one being ordered — so each
            # EXACT spelling occurs once. The verbatim-span invariant
            # notices; a case-insensitive matcher would not have.
            #
            # No status_word here: ATTRIBUTE_LEGALITY allows it on
            # MEDICATION and PROCEDURE only, which is the limitation the
            # xfail at the bottom of this file records.
            "type": "lab_vital",
            "text": "HbA1c",
            "occurrence": 1,
            "attributes": [],
        },
    ],
    "relations": [],
    "shared_triggers": [],
}


@pytest.fixture(scope="module")
def rxnorm(tmp_path_factory):
    from hdh.modules.rxnorm.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("scenarios") / "s.db"))
    session = get_session(engine)
    run_load(session, RX_FIXTURE)
    yield get_ontology_service("rxnorm", session)
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def extraction():
    return comprehend_text(SCENARIO_A, stub_extractor(SCENARIO_A_RAW))


# ── what the note is, structurally ───────────────────────────────────────


def test_the_note_has_no_soap_headers_at_all(extraction):
    """The premise of the whole scenario: this is prose, which is how most
    real notes look. It used to mean no orders were produced at all."""
    from hdh.modules.comprehension.contracts import SectionKind

    kinds = {section.kind for section in extraction.sections}
    assert kinds == {SectionKind.UNKNOWN}
    assert SectionKind.PLAN not in kinds


def test_an_unstructured_note_still_produces_orders(extraction):
    """The fix M5 owed §10: with no plan section, the STATUS WORD says what
    the section cannot — "continued", "added", "repeat"."""
    from hdh.modules.comprehension.applier import _in_plan
    from hdh.modules.comprehension.contracts import MentionType
    from hdh.modules.comprehension.pipeline import ComprehendedMention

    orderable = [
        mention.text
        for mention in extraction.mentions
        if mention.mention_type in (MentionType.MEDICATION, MentionType.LAB_VITAL)
        and _in_plan(
            type("N", (), {"extraction": extraction})(),
            ComprehendedMention(mention=mention, code=None, assertion=None, confidence=0.0),
        )
    ]
    assert "Blorbizide" in orderable and "Zorbex" in orderable
    # the FIRST HbA1c is a result stated in prose, not an order — it has no
    # ordering status word, and must not become a request
    assert orderable.count("Hba1c") == 0


# ── the medication rows: RxNorm's acceptance for this scenario ───────────


def test_the_generic_keeps_its_release_form(rxnorm):
    """ "Metformin ER" is not "Metformin": ER 500 MG is a different product
    and a different RxCUI. Losing the ER is losing the drug."""
    coding = resolve(
        rxnorm,
        "Blorbizide",
        strength=parse_strength("2 x 10mg"),
        raw="continued Blorbizide ER 2 x 10mg with evening meal",
    )
    assert coding.rxcui == rx.BLORBIZIDE_10_ER
    assert "Extended Release" in coding.display


def test_quantity_times_strength_does_not_become_the_strength(rxnorm):
    """ "2 x 500mg" is 500 MG of product taken twice. The dose is not the
    strength, and coding the product of the two hunts for something that
    may not exist."""
    assert parse_strength("2 x 10mg") == "10 MG"
    assert parse_strength("2 x 500mg") == "500 MG"


def test_the_brand_carries_the_branded_code(rxnorm):
    """§11 Q5. The real note says "Junovia" — a brand, misspelt. Recovering
    the spelling needs the trigram rung, which only PostgreSQL has, so the
    MISSPELLING is exercised in tests/test_postgres.py; what is asserted
    here is that a brand resolves to the branded product."""
    coding = resolve(rxnorm, "Zorbex", strength="10 MG", raw="added Zorbex 10 mg OD")
    assert (coding.rxcui, coding.tty) == (rx.ZORBEX_10_TAB, "SBD")


def test_the_ingredient_stays_one_hop_away(rxnorm):
    """Coding branded does not lose the analysis: a patient on Zorbex is on
    blorbizide, and a reconciliation has to be able to see it."""
    coding = resolve(rxnorm, "Zorbex", strength="10 MG", raw="Zorbex 10 mg OD")
    assert rx.BLORBIZIDE_IN in {c.code for c in rxnorm.ingredients_of(coding.rxcui)}


# ── what this scenario still cannot do ───────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="'higher than 7 HbA1c' is a RESULT with a comparator, stated in prose. "
    "The extractor gives the value but nothing carries the '>' — comprehension "
    "would need a comparator attribute kind (§10 row 3).",
)
def test_a_comparator_in_prose_is_captured(extraction):
    """The column exists (LabResult.comparator, milestone A). Nothing fills
    it from a note yet, and pretending otherwise would be the corpus
    quietly not asking."""
    hba1c = next(m for m in extraction.mentions if m.text == "Hba1c")
    kinds = {attribute.kind.value for attribute in hba1c.attributes}
    assert "comparator" in kinds


@pytest.mark.xfail(
    strict=True,
    reason="Condition.controlled is never written from a note. The attribute kind "
    "exists, ATTRIBUTE_LEGALITY allows it on PROBLEM, and the extractor prompt asks "
    "for it by name — the APPLIER is what drops it: _apply_conditions builds its "
    "Condition without ever reading AttributeKind.CONTROL (§10 row 2).",
)
def test_a_control_qualifier_reaches_the_problem_list():
    """The last mile, not the first.

    An earlier version of this test asserted the EXTRACTOR produced no
    control attribute, which was circular — the stub is ours, so it only
    proved what we had written into it. The real break is downstream, so
    this one hands the applier exactly what the prompt asks the model for
    and checks whether it survives to the chart.
    """
    from hdh.core.models import ConditionStatus, Patient, Sex, Visit, VisitType
    from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
    from hdh.modules.comprehension.pipeline import comprehend_note

    bootstrap_schema()
    session = get_session(get_engine(":memory:"))
    patient = Patient(
        mrn="MRN-CONTROL",
        first_name="Well",
        last_name="Treated",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 22), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()

    note = "A: well treated hypertension."
    raw = {
        "mentions": [
            {
                "type": "problem",
                "text": "hypertension",
                "occurrence": 1,
                # exactly what extract.py's rule 4 instructs the model to emit
                "attributes": [{"kind": "control", "text": "well treated", "occurrence": 1}],
            }
        ],
        "relations": [],
        "shared_triggers": [],
    }
    comprehended = comprehend_note(session, comprehend_text(note, stub_extractor(raw)))
    apply_to_chart(session, patient, comprehended, VisitTarget(visit=visit))

    charted = [c for c in patient.conditions if c.status is ConditionStatus.ACTIVE]
    assert charted, "the condition never reached the chart at all"
    assert charted[0].controlled is True


@pytest.mark.xfail(
    strict=True,
    reason="A LAB order in an unstructured note cannot be recognised. The status "
    "word is what says 'this was ordered' when there is no plan section, and "
    "ATTRIBUTE_LEGALITY allows status_word on MEDICATION and PROCEDURE only — so "
    "'asked for repeat HbA1c' has no way to distinguish itself from the result "
    "mentioned two clauses earlier. Extending the schema is a comprehension "
    "change, not an RxNorm one (§10).",
)
def test_a_lab_order_in_an_unstructured_note_is_recognised(extraction):
    """The medication half of Scenario A works; this half does not, and
    saying so is the point of keeping the scenario whole."""
    from hdh.modules.comprehension.applier import _in_plan
    from hdh.modules.comprehension.pipeline import ComprehendedMention

    ordered_lab = next(m for m in extraction.mentions if m.text == "HbA1c")
    assert _in_plan(
        type("N", (), {"extraction": extraction})(),
        ComprehendedMention(mention=ordered_lab, code=None, assertion=None, confidence=0.0),
    )
