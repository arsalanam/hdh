"""The shared funnel, tested without any licensed vocabulary.

These are the rules every vocabulary now inherits, so they are worth
holding down here rather than three times over in the module suites. The
full-edition measurements stay in tests/test_snomed_funnel_robustness.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.core.termsearch import (
    PARTIAL_COVERAGE_CEILING,
    SearchProfile,
    covers_every_word,
    looks_like_abbreviation,
    search,
)

VOCAB_A = "testvocab_a"
VOCAB_B = "testvocab_b"


@pytest.fixture(scope="module")
def two_vocabularies(tmp_path_factory):
    """Two vocabularies that deliberately share a term.

    Namespace isolation is the property most easily broken by a shared
    funnel and least likely to be noticed: a leak returns a real concept
    from the wrong catalog, which reads as a plausible answer.
    """
    bootstrap_schema()
    from hdh.core.models import Base

    engine = get_engine(str(tmp_path_factory.mktemp("termsearch") / "ts.db"))
    session = get_session(engine)
    concepts = Base.metadata.tables["ontology_concepts"]
    terms = Base.metadata.tables["ontology_terms"]

    rows = [
        (VOCAB_A, "A1", "Wobbling", {"tag": "finding"}),
        (VOCAB_A, "A2", "Wobbling gait disorder", {"tag": "disorder"}),
        (VOCAB_B, "B1", "Wobbling", {"tag": "finding"}),
    ]
    session.execute(
        insert(concepts),
        [
            {
                "id": f"{ontology}:{code}",
                "ontology": ontology,
                "code": code,
                "kind": "concept",
                "display": display,
                "properties": props,
            }
            for ontology, code, display, props in rows
        ],
    )
    session.execute(
        insert(terms),
        [
            {
                "concept_id": f"{ontology}:{code}",
                "term": display,
                "term_type": "preferred",
                "language": "en",
                "active": True,
                "properties": {},
            }
            for ontology, code, display, _props in rows
        ],
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_a_search_never_crosses_into_another_vocabulary(two_vocabularies):
    """The leak this most easily springs returns a real concept from the
    wrong catalog, which reads as a plausible answer."""
    hits = search(two_vocabularies, SearchProfile(ontology=VOCAB_A), "Wobbling")
    assert hits, "the funnel found nothing at all"
    assert {h.concept.ontology for h in hits} == {VOCAB_A}
    assert "B1" not in {h.concept.code for h in hits}


def test_the_module_hook_can_move_a_candidate(two_vocabularies):
    """`adjust` is how a module contributes what only it can compute —
    SNOMED's semantic tag and subtree, LOINC's specimen axis."""

    def prefer_disorders(concept, context):
        return [(0.5, "tag: disorder")] if concept.properties.get("tag") == "disorder" else []

    plain = search(two_vocabularies, SearchProfile(ontology=VOCAB_A), "Wobbling")
    boosted = search(
        two_vocabularies,
        SearchProfile(ontology=VOCAB_A, adjust=prefer_disorders),
        "Wobbling",
    )

    assert plain[0].concept.code == "A1"  # the exact term wins on its own
    assert boosted[0].concept.code == "A2"  # the hook overturns it
    assert "tag: disorder" in boosted[0].reason


def test_an_empty_mention_returns_nothing(two_vocabularies):
    assert search(two_vocabularies, SearchProfile(ontology=VOCAB_A), "   ") == ()


# ── the rules the funnel carries for everyone ────────────────────────────


def test_a_partial_match_can_never_be_charted():
    """The ceiling exists to sit below the pipeline's review threshold; if
    that coupling breaks, a half-matched guess reaches a chart."""
    from hdh.modules.comprehension.pipeline import REVIEW_THRESHOLD

    assert PARTIAL_COVERAGE_CEILING < REVIEW_THRESHOLD


def test_a_typo_covers_the_mention_but_a_lay_phrase_does_not():
    """The rule that separates "hypertenison" (chart it) from "sugar
    diabetes" (do not). Similarity over the whole string CANNOT make this
    call — measured on the full edition, typos span 0.35-0.71 and wrong
    answers 0.33-0.56 — so coverage is judged per word."""
    assert covers_every_word("hypothyroidsm", "Hypothyroidism")
    assert covers_every_word("astma", "Asthma")
    assert covers_every_word("hypertenison", "Hypertensive disorder")

    assert not covers_every_word("sugar diabetes", "Bronze diabetes")
    assert not covers_every_word("underactive thyroid", "Underactive infant")
    assert not covers_every_word("smoker's lung", "Smoker")


def test_abbreviation_shape_is_recognised():
    """Only a short alphanumeric token can head an "ABBR - Expansion"."""
    assert looks_like_abbreviation("SOB")
    assert looks_like_abbreviation("t2dm")
    assert not looks_like_abbreviation("shortness of breath")  # a phrase
    assert not looks_like_abbreviation("hypothyroidism")  # a word
    assert not looks_like_abbreviation("b")  # too short to disambiguate
    # keeps the LIKE pattern literal: no wildcard can reach the query
    assert not looks_like_abbreviation("a%b")
    assert not looks_like_abbreviation("b/p")


def test_a_vocabulary_without_abbreviation_terms_skips_that_rung():
    """LOINC keeps abbreviations as ordinary synonyms in RELATEDNAMES2, so
    it has no "ABBR - Expansion" shape to look for. The default is None so
    a module has to opt IN rather than inherit SNOMED's convention."""
    assert SearchProfile(ontology="x").abbreviation_separator is None
