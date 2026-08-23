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


def test_a_lab_value_in_prose_never_becomes_a_lab_result(extraction):
    """§10.0, stated as a guard rather than a paragraph.

    "came with higher than 7 Hba1c" has no specimen, no method, no
    reference range and no performing lab — it is a value being REFERRED
    TO as evidence. Charting it as a LabResult would manufacture a
    measurement record out of a sentence, and the chart would then hold two
    kinds of row that look identical and are not.

    This passes today because the applier refuses; the test exists so that
    it keeps refusing. Results come from a partner, through interchange,
    with an order to match against.
    """
    from hdh.core.models import ConditionStatus, LabResult, Patient, Sex, Visit, VisitType
    from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
    from hdh.modules.comprehension.pipeline import comprehend_note

    bootstrap_schema()
    engine = get_engine(":memory:")
    session = get_session(engine)
    patient = Patient(
        mrn="MRN-PROSE-LAB",
        first_name="Referred",
        last_name="To",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 22), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()

    comprehended = comprehend_note(session, extraction)
    result = apply_to_chart(session, patient, comprehended, VisitTarget(visit=visit))

    assert session.query(LabResult).count() == 0, "a note manufactured a measurement record"
    # and it says so rather than dropping it without trace
    assert any(v.kind in ("lab", "vitals") for v in result.verdicts)
    assert ConditionStatus  # the import is the point of the assertion above
    session.close()
    engine.dispose()


@pytest.mark.xfail(
    strict=True,
    reason="A referred-to lab value should land as evidence about the CONDITION "
    "(§10.0) — 'higher than 7 HbA1c' in a diabetic means uncontrolled diabetes. "
    "Nothing connects the two today: the value is extracted as a LAB_VITAL and the "
    "control flag is only written from an explicit control phrase on a PROBLEM, so "
    "the clinical meaning of the number is lost.",
)
def test_a_referred_lab_value_becomes_evidence_about_the_condition(extraction):
    """The replacement for what used to be 'the comparator is not captured'.

    Under §10.0 the comparator was the wrong thing to ask for: there is no
    LabResult for it to sit on. What is actually missing is the LINK — the
    number is about the diabetes, and nothing carries that.
    """
    from hdh.modules.comprehension.contracts import AttributeKind, MentionType

    diabetes = next(m for m in extraction.mentions if m.text == "type 2 diabetes")
    assert diabetes.mention_type is MentionType.PROBLEM
    assert any(a.kind is AttributeKind.CONTROL for a in diabetes.attributes)


@pytest.mark.xfail(
    strict=True,
    reason="A LAB order in an unstructured note cannot be recognised. The status "
    "word is what says 'this was ordered' when there is no plan section, and "
    "ATTRIBUTE_LEGALITY allows status_word on MEDICATION and PROCEDURE only — so "
    "'asked for repeat HbA1c' has no way to distinguish itself from the value "
    "referred to two clauses earlier. Under §10.0 this is now the ONLY lab-shaped "
    "thing a note can produce, so allowing it cannot blur order with result.",
)
def test_a_lab_order_in_an_unstructured_note_is_recognised(extraction):
    from hdh.modules.comprehension.applier import _in_plan
    from hdh.modules.comprehension.pipeline import ComprehendedMention

    ordered_lab = next(m for m in extraction.mentions if m.text == "HbA1c")
    assert _in_plan(
        type("N", (), {"extraction": extraction})(),
        ComprehendedMention(mention=ordered_lab, code=None, assertion=None, confidence=0.0),
    )


def test_a_control_qualifier_reaches_the_problem_list(control_world):
    """The last mile, and it is closed now.

    An earlier version of this test asserted the EXTRACTOR produced no
    control attribute, which was circular — the stub is ours, so it only
    proved what we had written into it. The real break was downstream:
    `_apply_conditions` built its Condition without ever reading
    AttributeKind.CONTROL, so the flag was never written from a note.

    The fabricated catalog has no control-qualified concept for blorbitis,
    which is the ORDINARY case (§10.0): the flag carries the meaning and
    the base code stands.
    """
    from hdh.core.models import ConditionStatus

    session, patient, base_code = control_world
    charted = [c for c in patient.conditions if c.status is ConditionStatus.ACTIVE]

    assert charted, "the condition never reached the chart at all"
    assert charted[0].controlled is True
    assert getattr(charted[0], "snomed_code", None) == base_code  # unrefined, honestly


@pytest.fixture(scope="module")
def control_world(tmp_path_factory):
    """A chart with the fabricated SNOMED catalog, and one controlled problem."""
    from sqlalchemy import insert

    from hdh.core.models import Base, ConditionStatus, Patient, Sex, Visit, VisitType
    from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
    from hdh.modules.comprehension.pipeline import comprehend_note
    from hdh.modules.snomed.loader import run_load

    snomed = Path(__file__).parent / "fixtures" / "snomed"
    sys.path.insert(0, str(snomed))
    import fixture_ids as snomed_ids

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("control") / "c.db"))
    session = get_session(engine)
    run_load(session, snomed)

    tables = Base.metadata.tables
    session.execute(
        insert(tables["ontology_concepts"]),
        [
            {
                "id": "icd10cm:B99.8",
                "ontology": "icd10cm",
                "code": "B99.8",
                "kind": "code",
                "display": "Other infectious disease",
            }
        ],
    )
    session.execute(
        insert(tables["ontology_edges"]),
        [
            {
                "source_id": "icd10cm:B99.8",
                "target_id": f"snomed_ct:{snomed_ids.CHRONIC_BLORBITIS}",
                "edge_type": "maps_to",
                "authority": "CURATED_DEMO",
                "confidence": 1.0,
                "properties": {},
            }
        ],
    )
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

    note = "A: well controlled Chronic blorbitis."
    raw = {
        "mentions": [
            {
                "type": "problem",
                "text": "Chronic blorbitis",
                "occurrence": 1,
                # exactly what extract.py's rule 4 instructs the model to emit
                "attributes": [{"kind": "control", "text": "well controlled", "occurrence": 1}],
            }
        ],
        "relations": [],
        "shared_triggers": [],
    }
    comprehended = comprehend_note(session, comprehend_text(note, stub_extractor(raw)))
    apply_to_chart(session, patient, comprehended, VisitTarget(visit=visit))
    assert ConditionStatus  # imported for the test body
    yield session, patient, snomed_ids.CHRONIC_BLORBITIS
    session.close()
    engine.dispose()
