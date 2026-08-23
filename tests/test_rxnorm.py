"""RxNorm: OntologyService #4, and the first vocabulary with no funnel.

The fixture is FABRICATED — RxNorm is redistributable only under UMLS
terms — so these RXCUIs are invented and the drug names are nonsense
words. What it reproduces faithfully is the shape of a release and the
graph of §4: a single-ingredient drug with a brand, and a two-ingredient
combination, which is where the compositional walk gets hard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.ontology import get_ontology_service
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.rxnorm.loader import RxNormLoadError, run_load

FIXTURE = Path(__file__).parent / "fixtures" / "rxnorm"
sys.path.insert(0, str(FIXTURE))
# NOT `fixture_ids`: the SNOMED fixture owns that name and every fixture
# directory joins sys.path, so a duplicate hands one suite another's codes.
import rxnorm_ids as fx  # noqa: E402


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("rxnorm") / "rx.db"))
    session = get_session(engine)
    run_load(session, FIXTURE)
    yield get_ontology_service("rxnorm", session)
    session.close()
    engine.dispose()


# ── the loader ───────────────────────────────────────────────────────────


def test_a_release_loads_concepts_terms_and_both_kinds_of_edge(tmp_path):
    bootstrap_schema()
    session = get_session(get_engine(str(tmp_path / "rx.db")))
    report = run_load(session, FIXTURE)
    assert report.concepts == 15
    assert report.ladder_edges == 13  # the specificity rungs, brands included
    assert report.attribute_edges == 4  # dose form, precise ingredient
    session.close()


def test_a_suppressed_atom_never_becomes_searchable(service):
    """A withdrawn name that stays searchable is worse than a missing one:
    a note mentioning it would code to a live concept."""
    assert service.normalize("Blorbizide OLD NAME") == ()
    assert service.normalize("Blorbizide", {"limit": 1})[0].concept.code == fx.BLORBIZIDE_IN


def test_source_atoms_become_synonyms(service):
    """Source atoms carry the names clinicians write rather than the
    normalized form, and they are where a funnel's recall comes from."""
    assert "Blorbizid" in service.synonyms(fx.BLORBIZIDE_IN)


def test_pointing_the_loader_at_the_wrong_place_fails_loudly(tmp_path):
    session = get_session(get_engine(str(tmp_path / "empty.db")))
    with pytest.raises(RxNormLoadError, match="RXNCONSO"):
        run_load(session, tmp_path)
    session.close()


# ── the ladder: what a drug IS, more generally ───────────────────────────


def test_a_branded_drug_is_its_clinical_drug_is_its_ingredient(service):
    """The chain a reconciliation needs: a patient on Zorbex is on
    blorbizide, and nothing else in the graph says so as plainly."""
    codes = {c.code for c in service.ancestors(fx.ZORBEX_10_TAB)}
    assert {fx.BLORBIZIDE_IN, fx.BLORBIZIDE_10_TAB, fx.BLORBIZIDE_10_SCDC} <= codes
    assert service.subsumes(fx.BLORBIZIDE_IN, fx.ZORBEX_10_TAB)


def test_subsumption_is_strict_and_does_not_cross_ingredients(service):
    assert not service.subsumes(fx.BLORBIZIDE_IN, fx.BLORBIZIDE_IN)  # strict
    assert not service.subsumes(fx.QUIXAMET_IN, fx.BLORBIZIDE_10_TAB)


def test_a_combination_has_every_ingredient_not_the_first_one(service):
    """Scenario C (§10): a patient on the combination is on BOTH drugs, and
    charting a second blorbizide beside it doubles an ingredient that is
    never named twice."""
    ingredients = {c.code for c in service.ingredients_of(fx.ZORBAMET_SBD)}
    assert ingredients == {fx.BLORBIZIDE_IN, fx.QUIXAMET_IN}


def test_brands_of_a_clinical_drug(service):
    assert {c.code for c in service.brands_of(fx.BLORBIZIDE_10_TAB)} == {fx.ZORBEX_10_TAB}


def test_typed_relations_are_reachable_and_directional(service):
    """The dose form is what separates the extended-release product from
    the plain one — "Metformin ER" is a different RxCUI from "Metformin"
    (§10 Scenario A)."""
    plain = service.attributes(fx.BLORBIZIDE_10_TAB)["has_dose_form"]
    extended = service.attributes(fx.BLORBIZIDE_10_ER)["has_dose_form"]
    assert [c.code for c in plain] == [fx.ORAL_TABLET]
    assert [c.code for c in extended] == [fx.ORAL_TABLET_ER]


# ── the funnel: shared, and steered by level ─────────────────────────────


def test_a_bare_drug_name_resolves_to_the_ingredient(service):
    """ "Lisinopril" alone means the ingredient — the level a problem list
    and an allergy list both work at."""
    top = service.normalize("Blorbizide", {"limit": 1})[0]
    assert top.concept.code == fx.BLORBIZIDE_IN
    assert top.concept.properties["tty"] == "IN"


def test_a_full_product_name_resolves_to_the_clinical_drug(service):
    top = service.normalize("Blorbizide 10 MG Oral Tablet", {"limit": 1})[0]
    assert top.concept.code == fx.BLORBIZIDE_10_TAB


def test_the_level_hint_is_what_makes_the_compositional_walk_work(service):
    """§5's lever: comprehension knows whether it extracted a strength and
    a form, so it can say which level the evidence supports instead of
    leaving a lexical funnel to guess from string length."""
    plain = service.normalize("Blorbizide", {"limit": 1})[0]
    steered = service.normalize("Blorbizide", {"levels": ["SCD"], "limit": 1})[0]

    assert plain.concept.properties["tty"] == "IN"
    assert steered.concept.properties["tty"] == "SCD"
    assert "level: SCD" in steered.reason


def test_a_brand_resolves_to_the_brand(service):
    """§11 Q5: a branded order carries the branded code, because what was
    prescribed is what the chart should say."""
    assert service.normalize("Zorbex", {"limit": 1})[0].concept.code == fx.ZORBEX_BN
    assert service.normalize("Zorbamet 10/500", {"limit": 1})[0].concept.code == fx.ZORBAMET_SBD


def test_an_unknown_drug_resolves_to_nothing(service):
    """Refuse-don't-guess: an uncoded request is legitimate state, and a
    wrong RxCUI is worse than none."""
    assert service.normalize("whole-body vibe tincture") == ()


def test_this_module_contains_no_funnel_of_its_own():
    """The point of M1 and M2. RxNorm is the first vocabulary to arrive
    after the extraction, and it must not have grown its own rungs — no
    ts_rank, no trigram, no coverage rule, no ceiling.
    """
    source = (Path(__file__).parent.parent / "src/hdh/modules/rxnorm/ontology.py").read_text(encoding="utf-8")
    for smell in ("ts_rank", "plainto_tsquery", "similarity(", "PARTIAL_COVERAGE", "difflib"):
        assert smell not in source, f"{smell} belongs in hdh.core.termsearch, not here"


# ── compositional coding (M4): §5's walk, and where it refuses ───────────


def _code(service, name, **kw):
    from hdh.modules.rxnorm.coding import resolve

    return resolve(service, name, **kw)


def test_a_name_and_a_strength_reach_the_clinical_drug(service):
    """§5's walk: lexical search finds the ingredient, the graph does the
    rest. The note never says "Oral Tablet" and does not have to."""
    coding = _code(service, "Blorbizide", strength="10 MG", route="PO", raw="Blorbizide 10mg PO daily")
    assert (coding.rxcui, coding.tty) == (fx.BLORBIZIDE_10_TAB, "SCD")


def test_extended_release_is_a_different_drug(service):
    """§10 Scenario A: "Metformin ER" is not "Metformin". Drop the ER and
    the code is a different product taken differently."""
    plain = _code(service, "Blorbizide", strength="10 MG", route="PO", raw="Blorbizide 10mg PO")
    extended = _code(service, "Blorbizide", strength="10 MG", route="PO", raw="Blorbizide ER 10mg PO")

    assert plain.rxcui == fx.BLORBIZIDE_10_TAB
    assert extended.rxcui == fx.BLORBIZIDE_10_ER
    assert plain.rxcui != extended.rxcui


def test_quantity_times_strength_is_not_the_strength(service):
    """§10 Scenario A: "2 x 500mg" means 500 MG of product taken twice.
    The note's arithmetic is not the label's, and coding the 1000 would
    look for a product that may not exist."""
    from hdh.modules.rxnorm.coding import parse_strength

    assert parse_strength("2 x 500mg") == "500 MG"
    assert parse_strength("10mg") == "10 MG"

    coding = _code(service, "Blorbizide", raw="Blorbizide ER 2 x 10mg with evening meal")
    assert coding.rxcui == fx.BLORBIZIDE_10_ER


def test_a_bare_drug_name_stops_at_the_ingredient(service):
    """The difference between "the deepest level the evidence supports"
    and "the deepest level reachable" is a chart full of doses nobody
    prescribed. A note naming only a drug supports only the ingredient."""
    coding = _code(service, "Blorbizide", raw="Blorbizide")
    assert (coding.rxcui, coding.tty) == (fx.BLORBIZIDE_IN, "IN")
    assert any("as deep as the evidence goes" in line for line in coding.evidence)


def test_a_strength_that_does_not_exist_refuses_rather_than_substitutes(service):
    """The worst thing this code could do is chart a dose the clinician
    never wrote. Falling through to another strength is exactly that."""
    coding = _code(service, "Blorbizide", strength="99 MG", route="PO", raw="Blorbizide 99mg PO")
    assert coding.rxcui == fx.BLORBIZIDE_IN  # stopped, did not substitute
    assert any("no product at 99 MG" in line for line in coding.evidence)


def test_a_brand_carries_the_branded_code(service):
    """§11 Q5: what was prescribed is what the chart should say, with the
    ingredient one graph hop away for any analysis that wants it."""
    coding = _code(service, "Zorbex", strength="10 MG", route="PO", raw="Zorbex 10 mg OD")
    assert (coding.rxcui, coding.tty) == (fx.ZORBEX_10_TAB, "SBD")
    assert fx.BLORBIZIDE_IN in {c.code for c in service.ingredients_of(coding.rxcui)}


def test_a_brand_without_a_strength_stops_at_the_brand(service):
    """Symmetric with the clinical walk: brands carry several strengths,
    so descending to one of them would invent the dose."""
    coding = _code(service, "Zorbex", raw="Zorbex")
    assert (coding.rxcui, coding.tty) == (fx.ZORBEX_BN, "BN")


def test_one_named_drug_does_not_code_to_a_combination(service):
    """Scenario C from the other direction. The combination really does
    contain 10 mg of blorbizide in an oral tablet, so strength and form
    alone cannot separate them — and coding a patient to a combination
    they were not prescribed adds a drug to their chart."""
    coding = _code(service, "Blorbizide", strength="10 MG", route="PO", raw="Blorbizide 10mg PO")
    assert coding.rxcui != fx.COMBO_SCD
    assert len(service.ingredients_of(coding.rxcui)) == 1


def test_an_unknown_drug_is_left_uncoded(service):
    """An uncoded order is legitimate state (§2); a wrong RxCUI is not."""
    assert _code(service, "whole-body vibe tincture", raw="whole-body vibe tincture") is None


def test_every_coding_records_why_it_stopped_where_it_did(service):
    """A code a reader cannot account for is a code they cannot trust."""
    coding = _code(service, "Blorbizide", strength="10 MG", route="PO", raw="Blorbizide 10mg PO")
    assert coding.evidence
    assert any("strength: 10 MG" in line for line in coding.evidence)
    assert any("name:" in line for line in coding.evidence)


# ── the agent surface (M5) ───────────────────────────────────────────────


def test_the_agent_tools_appear_only_when_a_catalog_is_loaded(tmp_path):
    """A tool that can only fail is worse than a missing one: the model
    will call it, read an error, and try to work around it."""
    pytest.importorskip("anthropic")
    from hdh.modules.rxnorm.agent_tools import build_rxnorm_tools

    bootstrap_schema()
    empty = get_session(get_engine(str(tmp_path / "empty.db")))
    assert build_rxnorm_tools(empty) == []
    empty.close()


def test_the_agent_tools_hold_no_decision_of_their_own(service):
    """Design §7: an agent tool may not contain a decision a non-agent
    caller would also need. `rxnorm_code_drug` must agree with
    `coding.resolve` exactly, because `hdh rxnorm code` uses the latter —
    a tool with its own copy makes the agent and the CLI diverge, and only
    one of them is tested.
    """
    pytest.importorskip("anthropic")
    import json

    from hdh.modules.rxnorm.agent_tools import build_rxnorm_tools
    from hdh.modules.rxnorm.coding import resolve

    tools = {tool.name: tool for tool in build_rxnorm_tools(service.session)}
    assert set(tools) == {"rxnorm_search", "rxnorm_code_drug", "rxnorm_ingredients", "rxnorm_brands"}

    answer = json.loads(tools["rxnorm_code_drug"].call({"mention": "Blorbizide ER 10mg", "route": "PO"}))
    direct = resolve(service, "Blorbizide", route="PO", raw="Blorbizide ER 10mg")
    assert answer["rxcui"] == direct.rxcui == fx.BLORBIZIDE_10_ER
    assert answer["evidence"] == list(direct.evidence)


def test_the_coding_tool_reports_a_refusal_rather_than_a_guess(service):
    """The refusal has to survive the wire, or the agent will read silence
    as "no answer" and invent one."""
    pytest.importorskip("anthropic")
    from hdh.modules.rxnorm.agent_tools import build_rxnorm_tools

    tools = {tool.name: tool for tool in build_rxnorm_tools(service.session)}
    answer = tools["rxnorm_code_drug"].call({"mention": "whole-body vibe tincture"})
    assert "uncoded" in answer.lower()


# ── comprehension reaches RxNorm (#73) ───────────────────────────────────


@pytest.fixture(scope="module")
def charting_world(tmp_path_factory):
    """A chart with the fabricated RxNorm release loaded, ready to apply a
    note against — the arrangement the normalizer's medication path needs
    and that no other suite provides."""
    from datetime import date

    from hdh.core.models import Patient, Sex, Visit, VisitType, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("rxchart") / "rx.db"))
    session = get_session(engine)
    run_load(session, FIXTURE)
    patient = Patient(
        mrn="MRN-RXNORM",
        first_name="Coded",
        last_name="Drug",
        date_of_birth=date(1962, 4, 4),
        sex=Sex.MALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 23), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.commit()
    yield session, patient, visit
    session.close()
    engine.dispose()


def _chart(session, patient, visit, text, raw):
    from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.extract import stub_extractor
    from hdh.modules.comprehension.pipeline import comprehend_note

    note = comprehend_note(session, comprehend_text(text, stub_extractor(raw)))
    return apply_to_chart(session, patient, note, VisitTarget(visit=visit))


def test_a_charted_prescription_carries_an_rxcui(charting_world):
    """The defect: RxNorm shipped M1–M5 and comprehension never called it.

    Medications resolved against the GENERATOR's drug-name table — not a
    terminology, no RXCUI, no ingredient, and a drug it does not list
    resolved to nothing at all. Prescriptions charted with
    `code_system=None, code=None` in columns migration 0009 added for this.
    """
    from hdh.core.models import Prescription

    session, patient, visit = charting_world
    text = "P: Continue Blorbizide 10 MG daily."
    raw = {
        "mentions": [
            {
                "type": "medication",
                "text": "Blorbizide",
                "occurrence": 1,
                "attributes": [
                    {"kind": "dose", "text": "10 MG", "occurrence": 1},
                    {"kind": "status_word", "text": "Continue", "occurrence": 1},
                ],
            }
        ]
    }
    _chart(session, patient, visit, text, raw)
    rx = session.query(Prescription).filter(Prescription.drug_name.ilike("%Blorbizide%")).one()
    assert rx.code_system == "rxnorm"
    assert rx.code == fx.BLORBIZIDE_10_TAB, "the strength in the note should reach the clinical drug"


def test_the_drug_name_stays_what_the_clinician_wrote(charting_world):
    """An RxNorm display is a full product string. Putting it in drug_name
    would duplicate the dose column into the name and break matching against
    every earlier visit — the code carries the precision instead."""
    from hdh.core.models import Prescription

    session, patient, visit = charting_world
    rx = session.query(Prescription).filter(Prescription.drug_name.ilike("%Blorbizide%")).one()
    assert rx.drug_name == "Blorbizide"
    assert rx.dose == "10 MG"


def test_a_drug_the_note_underspecifies_is_not_coded_deeper_than_the_evidence(charting_world):
    """resolve's judgement has to survive the trip through comprehension: a
    bare name must not descend to a product by inventing a strength."""
    from hdh.core.models import Prescription

    session, patient, visit = charting_world
    raw = {"mentions": [{"type": "medication", "text": "Quixamet", "occurrence": 1, "attributes": []}]}
    _chart(session, patient, visit, "P: Start Quixamet.", raw)
    rx = session.query(Prescription).filter(Prescription.drug_name == "Quixamet").one()
    assert rx.code == fx.QUIXAMET_IN, "a bare name is an ingredient, not a product"


def test_without_a_catalog_nothing_changes(tmp_path):
    """RxNorm is licensed, so most installations will not have it. The
    generator's name table stays as the offline fallback and a chart built
    without a catalog must look exactly as it did before."""
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.comprehension.contracts import Mention, MentionType, Span
    from hdh.modules.comprehension.normalize import MentionNormalizer

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "nocat.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)

    normalizer = MentionNormalizer(session)
    assert normalizer._rxnorm is None
    mention = Mention(
        id=0,
        mention_type=MentionType.MEDICATION,
        span=Span(0, 10),
        text="Lisinopril",
        section_id=0,
        attributes=(),
    )
    codes = normalizer.candidates(mention)
    assert not codes or codes[0].system == "drug-catalog"
    session.close()
    engine.dispose()
