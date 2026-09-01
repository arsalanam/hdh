"""Reranking a shortlist (#100).

Tested with a deterministic word-overlap reranker so CI needs no AWS
account. That fake proves the pipeline reorders and proves nothing about
quality — the same discipline as `HashingEmbedder`.

The behaviour worth most attention is the fallback: a reranker is a paid
network call, and when it fails the plan must still be buildable from the
vector order. Ordering is a refinement; evidence is not.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import rerank
from hdh.modules.careplan.knowledge import KnowledgeHit, RerankedVectorStore
from hdh.modules.careplan.rerank import (
    CANDIDATE_MULTIPLIER,
    CohereReranker,
    LexicalReranker,
    RerankError,
    available,
    build_reranker,
)

PASSAGES = [
    "Sulfonylureas lower glucose whether or not the patient has eaten.",
    "NSAIDs raise bleeding risk when an anticoagulant is already prescribed.",
    "Two drugs from the same class rarely add benefit and reliably add risk.",
]


# ── the registry ─────────────────────────────────────────────────────────


def test_both_rerankers_are_registered():
    assert {"cohere", "lexical"} <= set(available())


def test_the_default_is_the_real_one():
    """A test fake must never be what a plan gets by accident."""
    assert rerank.DEFAULT == "cohere"


def test_an_unknown_reranker_lists_what_exists():
    with pytest.raises(RerankError, match="cohere"):
        build_reranker("magic")


def test_the_model_is_pinned():
    """A different reranker orders differently, so a measurement taken under
    one does not carry to another."""
    assert rerank.COHERE_MODEL == "cohere.rerank-v3-5:0"


# ── the fake, and the contract every reranker keeps ──────────────────────


def test_reranking_returns_indexes_into_what_it_was_given():
    """The caller maps these back onto its own hits, so an index outside the
    range would attach a score to the wrong chunk."""
    ordered = LexicalReranker().rerank("bleeding risk anticoagulant", PASSAGES, top_n=3)
    assert ordered
    assert all(0 <= index < len(PASSAGES) for index, _score in ordered)
    assert len({index for index, _score in ordered}) == len(ordered), "an index repeated"


def test_the_best_passage_comes_first():
    ordered = LexicalReranker().rerank("bleeding risk anticoagulant", PASSAGES, top_n=3)
    assert ordered[0][0] == 1


def test_top_n_is_respected():
    assert len(LexicalReranker().rerank("risk", PASSAGES, top_n=2)) == 2


def test_scores_come_back_descending():
    ordered = LexicalReranker().rerank("bleeding risk", PASSAGES, top_n=3)
    assert [s for _i, s in ordered] == sorted((s for _i, s in ordered), reverse=True)


def test_no_passages_is_not_an_error():
    assert LexicalReranker().rerank("anything", [], top_n=5) == []


# ── Cohere, without calling it ───────────────────────────────────────────


class _FakeRerankClient:
    def __init__(self, results, calls=None):
        self.results = results
        self.calls = calls if calls is not None else []

    def rerank(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": self.results}


def test_cohere_sends_the_query_and_every_passage_inline():
    calls: list = []
    client = _FakeRerankClient([{"index": 0, "relevanceScore": 0.9}], calls)
    CohereReranker(client=client, region="us-east-1").rerank("q", PASSAGES, top_n=2)
    sent = calls[0]
    assert sent["queries"][0]["textQuery"]["text"] == "q"
    assert len(sent["sources"]) == len(PASSAGES)
    config = sent["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert config["numberOfResults"] == 2
    assert "cohere.rerank-v3-5:0" in config["modelConfiguration"]["modelArn"]


def test_cohere_never_asks_for_more_results_than_it_sent():
    calls: list = []
    client = _FakeRerankClient([], calls)
    CohereReranker(client=client, region="us-east-1").rerank("q", PASSAGES[:2], top_n=10)
    config = calls[0]["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert config["numberOfResults"] == 2


def test_an_out_of_range_index_is_refused():
    """A score attached to the wrong chunk would cite the wrong document —
    silently, and in a plan whose whole value is traceability."""
    client = _FakeRerankClient([{"index": 99, "relevanceScore": 0.9}])
    with pytest.raises(RerankError, match="wrong document"):
        CohereReranker(client=client, region="us-east-1").rerank("q", PASSAGES, top_n=3)


# ── the two-stage store ──────────────────────────────────────────────────


class _FakeVectors:
    """Stands in for the vector arm, recording what k it was asked for."""

    def __init__(self, hits):
        self.hits = hits
        self.asked_for: list[int] = []

    def search(self, query, corpus, k=5, filters=None):
        self.asked_for.append(k)
        return self.hits[:k]


def _hit(doc_id: str, chunk: str, score: float = 0.5) -> KnowledgeHit:
    return KnowledgeHit(
        corpus="med_safety",
        doc_id=doc_id,
        chunk=chunk,
        score=score,
        source="s",
        license="MIT",
        metadata={},
    )


def _store(hits, reranker):
    store = RerankedVectorStore(session=None, reranker=reranker)
    store._vectors = _FakeVectors(hits)
    return store


def test_the_shortlist_is_wider_than_k():
    """A reranker only shown the top five can never promote the chunk the
    embedding ranked sixth — which is the case it exists for."""
    hits = [_hit(f"d{i}", f"chunk {i}") for i in range(20)]
    store = _store(hits, LexicalReranker())
    store.search("chunk 7", "med_safety", k=3)
    assert store._vectors.asked_for == [3 * CANDIDATE_MULTIPLIER]


def test_the_reranker_can_promote_something_the_vector_ranked_low():
    """The whole point of the second stage."""
    hits = [
        _hit("first", "unrelated text about nothing", 0.9),
        _hit("second", "also unrelated", 0.8),
        _hit("buried", "bleeding risk with an anticoagulant", 0.3),
    ]
    result = _store(hits, LexicalReranker()).search("bleeding risk anticoagulant", "med_safety", k=2)
    assert result[0].doc_id == "buried"


def test_the_hit_keeps_its_citation_after_reordering():
    """Reranking changes the order and the score, never which document a
    chunk came from."""
    hits = [_hit("a", "bleeding risk"), _hit("b", "something else")]
    result = _store(hits, LexicalReranker()).search("bleeding risk", "med_safety", k=2)
    assert {h.doc_id for h in result} == {"a", "b"}
    assert result[0].citation() == "med_safety/a"


class _BrokenReranker:
    name = "broken"

    def rerank(self, query, passages, top_n):
        raise RuntimeError("bedrock is having a day")


def test_a_failed_rerank_falls_back_to_the_vector_order(caplog):
    """A plan built on slightly worse ordering is still a plan; a plan built
    on no evidence is not."""
    hits = [_hit("a", "first"), _hit("b", "second"), _hit("c", "third")]
    result = _store(hits, _BrokenReranker()).search("query", "med_safety", k=2)
    assert [h.doc_id for h in result] == ["a", "b"]


def test_the_fallback_is_logged_rather_than_swallowed(caplog):
    import logging

    hits = [_hit("a", "first"), _hit("b", "second")]
    with caplog.at_level(logging.WARNING):
        _store(hits, _BrokenReranker()).search("query", "med_safety", k=2)
    assert "falling back" in caplog.text
    assert "bedrock is having a day" in caplog.text


def test_a_single_hit_is_not_worth_a_model_call():
    hits = [_hit("only", "one chunk")]
    store = _store(hits, _BrokenReranker())
    assert len(store.search("query", "med_safety", k=3)) == 1


def test_nothing_retrieved_stays_nothing():
    """Reranking cannot manufacture evidence that retrieval did not find."""
    assert _store([], LexicalReranker()).search("query", "med_safety", k=3) == []


def test_the_retriever_is_registered_as_built():
    from hdh.modules.careplan import retriever

    assert retriever.catalogue()["vector+rerank"][0] is True
