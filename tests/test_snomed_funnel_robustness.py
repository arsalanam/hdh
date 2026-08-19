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
one kind is dangerous — see `test_a_wrong_answer_must_not_be_confident`.
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

#: The frontier. These are surfaces a patient or a hurried clinician
#: writes, whose concept exists but whose term set lacks the phrasing.
#: They are xfail rather than deleted because they document exactly where
#: lexical retrieval stops — and if synonym enrichment (UMLS terms, a
#: curated abbreviation table like #41's symptom map) or dense retrieval
#: lands, these start passing and say so.
FRONTIER: tuple[tuple[str, str, str], ...] = (
    ("SOB", "267036007", "resolves to 'Sobbing respiration' at 1.00 — an exact term collision"),
    ("afib", "49436004", "resolves to 'Afipia' (a bacterium) at 0.28"),
    ("sugar diabetes", "44054006", "resolves to 'Bronze diabetes' (haemochromatosis) at 0.61"),
    ("underactive thyroid", "40930008", "resolves to 'Underactive infant' at 0.63"),
    ("can't catch my breath", "267036007", "resolves to 'Catching breath' at 1.00"),
    ("smoker's lung", "13645005", "resolves to 'Smoker' at 0.67"),
    ("diabetis", "73211009", "resolves to 'Iritis due to diabetes mellitus' at 0.66"),
)

#: A ratchet, not a target. These surfaces currently return a WRONG
#: concept at chartable confidence. The count may fall but must never
#: rise: a new entry is a regression that would put a wrong code on a
#: chart. Lower it as synonym coverage improves.
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

    Currently VIOLATED by exact-term collisions ("SOB" matches the
    preferred term of 'Sobbing respiration' at 1.00). Rather than fail
    forever on known debt it is a RATCHET: every offender is reported,
    and the count may fall but never rise.
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
@pytest.mark.xfail(strict=False, reason="lexical retrieval's known frontier — see module docstring")
def test_frontier_surfaces(service, surface, expected, why):
    """Documents where lexical retrieval stops. Not strict: when synonym
    enrichment or dense retrieval lands, these pass and announce it."""
    top = _top(service, surface)
    assert top is not None and top.concept.code == expected, f"{surface!r}: {why}"
