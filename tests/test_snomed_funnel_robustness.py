"""How far does the LEXICAL funnel carry us? (RFC #21 Q2 evidence.)

Our eval reports ~97% linking accuracy, but that number cannot answer
"is FTS enough?" — the corpus renders notes from the same condition
catalog the codes come from, so the funnel is matching near-verbatim
strings. This suite poses the question the corpus never does: surfaces a
real note contains but a terminology's own term set may not.

Marked ``fullload``: meaningful only against a licensee's loaded SNOMED
CT edition (386k concepts / 1M terms). Never runs in CI, costs no API
calls, and is the measurement that should decide whether dense retrieval
is ever needed — instead of intuition.

    HDH_SNOMED_DB_URL=postgresql+psycopg://... uv run pytest -m fullload

Measured 2026-08-19 on the US Edition: 4/4 verbatim, 3/3 misspelling,
3/6 abbreviation, 0/5 lay phrasing. The failures split in two, and only
one kind is dangerous — see `test_a_wrong_answer_must_not_be_confident`
and issue #54, which tracks closing the dangerous half.

A caution this suite exists to enforce: "is the term in the term set?" is
NOT the question. `SOB` fails while SNOMED carries `SOB - Shortness of
breath` on the very concept we want — the funnel retrieves it and then
ranks it second. Always ask WHERE THE TRUTH RANKED before concluding a
surface needs new vocabulary; see the cause tags on FRONTIER below.
"""

import os

import pytest

pytestmark = pytest.mark.fullload

DIABETES = "73211009"  # Diabetes mellitus (disorder) — the load probe

#: Surfaces the funnel MUST resolve: verbatim clinical text, and the
#: abbreviations and lay terms SNOMED itself carries as synonyms. A
#: regression here means the funnel's ranking broke (the class of bug
#: that once resolved "fatigue" to "Exercise induced muscle fatigue").
MUST_RESOLVE: tuple[tuple[str, str], ...] = (
    ("Essential hypertension", "59621000"),
    ("myocardial infarction", "22298006"),
    ("heart attack", "22298006"),  # a SNOMED synonym, not a guess
    ("Type 2 diabetes mellitus", "44054006"),
    ("T2DM", "44054006"),
    ("MI", "22298006"),
    ("COPD", "13645005"),
    ("shortness of breath", "267036007"),
)

#: Trigram's job: a clinician's typo must still land.
MISSPELLINGS: tuple[tuple[str, str], ...] = (
    ("hypothyroidsm", "40930008"),  # -> Hypothyroidism, 0.73
    ("astma", "195967001"),  # -> Asthma, 0.63
    # A bare misspelling of a disease NAME lands on the generic concept
    # rather than a subtype, which is the right answer to give when the
    # text itself says no more than "hypertension".
    ("hypertenison", "38341003"),  # -> Hypertensive disorder, 0.64
)

#: The frontier: surfaces a patient or a hurried clinician writes that we
#: get wrong today. Each entry records WHICH of three causes it is, so the
#: next reader does not re-derive it (issue #54 has the measurements):
#:
#:   RANKING   — the right concept IS retrieved and loses the sort. No
#:               added vocabulary can fix it; the ranking must change.
#:   THRESHOLD — the right concept is excluded before ranking ever runs.
#:   VOCABULARY— the phrasing genuinely is not in the term set. This is
#:               the only class where semantic matching could help.
#:
#: They are xfail rather than deleted because they document exactly where
#: the funnel stops — when a fix lands, these start passing and say so.
FRONTIER: tuple[tuple[str, str, str], ...] = (
    # RANKING: Dyspnea carries 'SOB - Shortness of breath' and is returned
    # at rank 2 (0.98). It loses to a STEMMING artifact — 'Sobbing' -> 'sob'
    # — partly because ts_rank normalization 1 penalises the longer term for
    # spelling the abbreviation out. Fix the ranking, not the vocabulary.
    ("SOB", "267036007", "RANKING: retrieved at rank 2/3 (0.98), loses to 'Sobbing respiration' 1.00"),
    # THRESHOLD: FTS finds nothing ('diabetis' stems to 'diabeti', 'diabetes'
    # to 'diabet'), so trigram runs — then pg_trgm's 0.3 cutoff drops
    # 'Diabetes mellitus' at 0.286 while 'Diabetic jam' clears it at 0.467.
    ("diabetis", "73211009", "THRESHOLD: truth at similarity 0.286, below the 0.3 trigram cutoff"),
    # VOCABULARY: the concept's term set has no lay phrasing at all. COPD,
    # for one, carries fifteen terms and every one of them is clinical.
    (
        "afib",
        "49436004",
        "VOCABULARY: not retrieved; 'Afipia' (a bacterium) at 0.28 — but SAFE, under review",
    ),
    ("sugar diabetes", "44054006", "VOCABULARY: not retrieved in 41 candidates; 'Bronze diabetes' at 0.61"),
    (
        "underactive thyroid",
        "40930008",
        "VOCABULARY: not retrieved in 24 candidates; 'Underactive infant' 0.63",
    ),
    ("can't catch my breath", "267036007", "VOCABULARY: not retrieved; 'Catching breath' at 1.00"),
    ("smoker's lung", "13645005", "VOCABULARY: not retrieved in 29 candidates; 'Smoker' at 0.67"),
)

#: A ratchet, not a target. These surfaces currently return a WRONG
#: concept at chartable confidence. The count may fall but must never
#: rise: a new entry is a regression that would put a wrong code on a
#: chart. Lower it as the causes above are fixed — ranking first (it needs
#: no new data at all), then vocabulary. See issue #54.
MAX_CONFIDENT_WRONG = 6

#: Below this, the pipeline routes a mention to human review rather than
#: charting it (pipeline.REVIEW_THRESHOLD).
REVIEW_THRESHOLD = 0.6


@pytest.fixture(scope="module")
def service():
    url = os.environ.get("HDH_SNOMED_DB_URL")
    if not url:
        pytest.skip("HDH_SNOMED_DB_URL not set — full-edition tests run only on a licensee's machine")
    from hdh.core.models import get_engine, get_session
    from hdh.core.ontology import get_ontology_service
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(db_url=url)
    session = get_session(engine)
    resolved = get_ontology_service("snomed_ct", session)
    if resolved.lookup(DIABETES) is None:
        pytest.skip("SNOMED US Edition not loaded in HDH_SNOMED_DB_URL")
    yield resolved
    session.close()
    engine.dispose()


def _top(service, surface: str):
    hits = service.normalize(surface, {"semantic_tags": ["disorder", "finding"], "limit": 5})
    return hits[0] if hits else None


@pytest.mark.parametrize("surface,expected", MUST_RESOLVE, ids=lambda v: v if isinstance(v, str) else "")
def test_clinical_and_synonymed_surfaces_resolve(service, surface, expected):
    """The funnel's core competence — and the regression guard on ranking."""
    top = _top(service, surface)
    assert top is not None, f"{surface!r}: the funnel returned nothing"
    assert top.concept.code == expected, f"{surface!r} -> {top.concept.display} ({top.concept.code})"


@pytest.mark.parametrize("surface,expected", MISSPELLINGS, ids=lambda v: v if isinstance(v, str) else "")
def test_misspellings_survive_via_trigram(service, surface, expected):
    top = _top(service, surface)
    assert top is not None and top.concept.code == expected, f"{surface!r} -> {top and top.concept.display}"


def test_a_wrong_answer_must_not_be_confident(service):
    """The contract that actually matters clinically.

    A miss is tolerable — it becomes a review item and a human decides.
    A miss reported at high confidence is charted, so it is not. Every
    lay/abbreviated surface must therefore either resolve correctly, or
    come back below the review threshold.

    Currently VIOLATED six times. The worst is not a vocabulary gap at
    all: "SOB" loses to 'Sobbing respiration' at 1.00 because the English
    stemmer maps 'Sobbing' -> 'sob', while the concept that genuinely owns
    the abbreviation is ranked second. Rather than fail forever on known
    debt this is a RATCHET: every offender is reported, and the count may
    fall but never rise.
    """
    confident_and_wrong = []
    for surface, expected, _note in FRONTIER:
        top = _top(service, surface)
        if top is None or top.concept.code == expected:
            continue
        if top.score >= REVIEW_THRESHOLD:
            confident_and_wrong.append(f"{surface!r} -> {top.concept.display!r} ({top.score:.2f})")

    assert len(confident_and_wrong) <= MAX_CONFIDENT_WRONG, (
        f"{len(confident_and_wrong)} surfaces return a wrong concept at chartable "
        f"confidence (the ratchet allows {MAX_CONFIDENT_WRONG}):\n  " + "\n  ".join(confident_and_wrong)
    )


@pytest.mark.parametrize(
    "surface,expected,why", FRONTIER, ids=lambda v: v if isinstance(v, str) and " " not in v[:4] else ""
)
def test_the_recorded_cause_is_still_the_real_cause(service, surface, expected, why):
    """Keeps FRONTIER's cause tags honest, instead of trusting a comment.

    The distinction that matters is whether the correct concept is
    RETRIEVED. A tag saying VOCABULARY while the truth is sitting at rank
    2 would send the next reader off to load synonyms for a bug that
    ranking owns — which is exactly the mistake issue #54 records.
    """
    hits = service.normalize(surface, {"semantic_tags": ["disorder", "finding"], "limit": 50})
    retrieved = any(h.concept.code == expected for h in hits)
    if why.startswith("RANKING"):
        rank = next(i for i, h in enumerate(hits, 1) if h.concept.code == expected)
        assert rank > 1, f"{surface!r} now ranks the truth first — promote it to MUST_RESOLVE"
        assert retrieved, f"{surface!r} is tagged RANKING but the truth is no longer retrieved"
    elif why.startswith("VOCABULARY"):
        assert not retrieved, (
            f"{surface!r} is tagged VOCABULARY but the truth IS retrieved "
            f"(rank {next(i for i, h in enumerate(hits, 1) if h.concept.code == expected)}) "
            "— it is a ranking problem, not a missing-term problem"
        )


@pytest.mark.parametrize(
    "surface,expected,why", FRONTIER, ids=lambda v: v if isinstance(v, str) and " " not in v[:4] else ""
)
@pytest.mark.xfail(strict=False, reason="lexical retrieval's known frontier — see module docstring")
def test_frontier_surfaces(service, surface, expected, why):
    """Documents where lexical retrieval stops. Not strict: when synonym
    enrichment or dense retrieval lands, these pass and announce it."""
    top = _top(service, surface)
    assert top is not None and top.concept.code == expected, f"{surface!r}: {why}"
