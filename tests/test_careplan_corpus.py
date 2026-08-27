"""The coverage gate: is the knowledge base big enough to plan from?

Before this existed, the answer was a matter of taste. It is not — the
condition catalog enumerates exactly which chronic conditions the generator
can produce, so *"can a plan cite anything about this patient's problems"*
is a question with a computable answer.

That matters because of what the retrieval funnel does when the answer is
no. `generate._kept()` drops any selection citing something that was not
offered, which is the right rule and has a consequence: **a plan can only
be about what the corpus can support.** The first live plan addressed one
problem out of ten and the grader marked it down for completeness — but
the corpus at the time held four chunks, all about sulfonylureas and
glycaemic targets. There was no plan available that would have scored
better. The grader was measuring the corpus and attributing it to the
generator.

So the gate runs in both directions. Every chronic condition needs a
document, and every document's claim of coverage has to be real — a
typo in the metadata would assert coverage that retrieval cannot deliver,
which is worse than an honest gap because nothing would report it.
"""

from __future__ import annotations

import pytest

from hdh.core.conditions import default_catalog
from hdh.modules.careplan.ingest import CorpusError, available, read_corpus
from hdh.modules.careplan.knowledge import chunk_document

#: Corpora whose documents must declare the conditions they cover.
CONDITION_CORPORA = ("condition_guidelines",)


def _chronic() -> dict[str, str]:
    """Chronic condition name → the ICD-10 code the generator writes."""
    catalog = default_catalog()
    return {
        profile.name: profile.icd10_code
        for profile in catalog._profiles.values()  # noqa: SLF001 — no public iterator yet
        if getattr(profile, "chronic", False)
    }


def _documents(corpus: str):
    _manifest, documents = read_corpus(corpus)
    return documents


def _claimed(corpus: str) -> dict[str, list[str]]:
    """Condition name → the documents claiming to cover it."""
    claims: dict[str, list[str]] = {}
    for document in _documents(corpus):
        for name in document.metadata.get("conditions", ()):
            claims.setdefault(str(name), []).append(document.doc_id)
    return claims


# ── the gate ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_every_chronic_condition_the_generator_produces_has_a_document(corpus):
    """The gate itself.

    A patient's problem with nothing citable behind it cannot appear in a
    plan at all — not as a weak concern, not as a flagged omission,
    nothing. It simply is not proposable. That is a silent failure, which
    is why it needs a loud test.
    """
    missing = sorted(set(_chronic()) - set(_claimed(corpus)))
    assert not missing, (
        f"{corpus} covers no knowledge for: {', '.join(missing)}. "
        "A plan for a patient with one of these cannot cite anything, so the "
        "concern cannot be proposed and the omission will read as a generator failure."
    )


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_no_document_claims_a_condition_the_catalog_does_not_have(corpus):
    """The other direction, and the more dangerous one.

    A misspelt condition name in front matter would satisfy the coverage
    test above while covering nothing. An honest gap is reported; a false
    claim of coverage is not, which makes it the worse failure.
    """
    unknown = sorted(set(_claimed(corpus)) - set(_chronic()))
    assert not unknown, (
        f"{corpus} claims coverage for conditions the catalog does not define: "
        f"{', '.join(unknown)} — check for a typo, or a condition that was renamed."
    )


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_every_document_declares_what_it_covers(corpus):
    """Metadata is how retrieval and this gate both find a document. One
    without it is invisible to both, however good the prose is."""
    for document in _documents(corpus):
        assert document.metadata.get("conditions"), f"{document.doc_id} declares no conditions"


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_declared_icd10_codes_match_the_catalog(corpus):
    """Catches drift the other tests cannot see.

    If a condition pack changes its ICD-10 code, the corpus metadata is
    silently wrong from that moment. Nothing else in the system would
    notice, because the code is not what retrieval matches on — it is what
    a reader would use to check the document against the chart.
    """
    chronic = _chronic()
    for document in _documents(corpus):
        declared = [str(code) for code in document.metadata.get("icd10", ())]
        if not declared:
            continue
        for name in document.metadata.get("conditions", ()):
            expected = chronic.get(str(name))
            if expected is None:
                continue
            assert expected in declared, (
                f"{document.doc_id} covers {name} but does not list its catalog code "
                f"{expected} (declares {', '.join(declared)})"
            )


# ── the corpus is usable, not just present ───────────────────────────────


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_the_corpus_reads_and_every_document_can_be_cited(corpus):
    """`read_corpus` refuses a document with no source. Asserting it here
    means the gate fails on an uncitable document rather than at ingest
    time on somebody else's machine."""
    _manifest, documents = read_corpus(corpus)
    assert documents
    for document in documents:
        assert document.source.strip()
        assert document.license.strip()


@pytest.mark.parametrize("corpus", CONDITION_CORPORA)
def test_documents_chunk_into_more_than_one_piece(corpus):
    """A document that yields a single chunk is retrieved whole or not at
    all, which wastes the ranking. Not a hard requirement — but a corpus
    where it is true of everything is one written as slogans rather than
    as material a plan can quote from."""
    yields = [len(chunk_document(document.text)) for document in _documents(corpus)]
    assert sum(yields) >= 2 * len(yields), f"{corpus} averages under two chunks per document"


def test_the_corpus_is_bundled_and_discoverable():
    """`available()` is what `hdh careplan ingest` iterates with no
    --corpus. A corpus on disk that it cannot see never gets loaded."""
    for corpus in CONDITION_CORPORA:
        assert corpus in available()


def test_a_corpus_that_does_not_exist_still_fails_loudly():
    """The gate must not pass by silently skipping a missing corpus."""
    with pytest.raises(CorpusError):
        read_corpus("no_such_corpus")
