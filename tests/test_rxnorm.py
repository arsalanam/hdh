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
    assert report.ladder_edges == 11  # the specificity rungs
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
