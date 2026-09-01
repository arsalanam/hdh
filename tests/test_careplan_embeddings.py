"""Embedders, behind a registry (#100).

The plumbing is tested with a hashing embedder so CI needs no AWS account.
That fake proves the pipeline moves vectors correctly and proves **nothing**
about whether retrieval got better — it has no idea that "bleeding risk"
and "haemorrhage" are related. The quality question needs Bedrock and the
cohort, and is deliberately a separate exercise.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import embeddings
from hdh.modules.careplan.embeddings import (
    DIMENSIONS,
    EmbedderError,
    HashingEmbedder,
    TitanEmbedder,
    available,
    build_embedder,
)

# ── the registry ─────────────────────────────────────────────────────────


def test_both_embedders_are_registered():
    assert {"titan", "hashing"} <= set(available())


def test_an_unknown_embedder_lists_what_exists():
    with pytest.raises(EmbedderError, match="titan"):
        build_embedder("word2vec")


def test_the_default_is_the_real_one():
    """A test fake must never be what a plan gets by accident."""
    assert embeddings.DEFAULT == "titan"


def test_the_embedder_can_be_chosen_by_environment(monkeypatch):
    monkeypatch.setenv(embeddings.ENV_VAR, "hashing")
    assert build_embedder().name == "hashing"


# ── the fake, which the rest of the suite depends on ─────────────────────


def test_hashed_vectors_are_the_declared_width():
    """A corpus embedded at two widths is not searchable, and the column has
    one width."""
    vectors = HashingEmbedder().embed(["one", "two"])
    assert [len(v) for v in vectors] == [DIMENSIONS, DIMENSIONS]


def test_hashed_vectors_are_stable_across_calls():
    """Ingest and query happen in different processes. A fake that drifted
    would make every vector test flaky for reasons unrelated to the code."""
    assert HashingEmbedder().embed(["aspirin"]) == HashingEmbedder().embed(["aspirin"])


def test_different_text_gives_different_vectors():
    first, second = HashingEmbedder().embed(["aspirin", "warfarin"])
    assert first != second


def test_hashed_vectors_are_unit_length():
    """So cosine similarity is a dot product, as it is for Titan."""
    (vector,) = HashingEmbedder().embed(["anything"])
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-9


def test_order_is_preserved():
    """The caller zips these back against the rows they came from."""
    embedder = HashingEmbedder()
    batch = embedder.embed(["a", "b", "c"])
    assert batch == [embedder.embed([t])[0] for t in ("a", "b", "c")]


# ── Titan, without calling it ────────────────────────────────────────────


class _FakeBedrock:
    """Enough of bedrock-runtime to check what we send and how we read it."""

    def __init__(self, vector, calls=None):
        self.vector = vector
        self.calls = calls if calls is not None else []

    def invoke_model(self, modelId, body):  # noqa: N803 - boto3's spelling
        import io
        import json

        self.calls.append((modelId, json.loads(body)))
        return {"body": io.BytesIO(json.dumps({"embedding": self.vector}).encode())}


def test_titan_asks_for_normalised_vectors_of_the_declared_width():
    calls: list = []
    client = _FakeBedrock([0.1] * DIMENSIONS, calls)
    TitanEmbedder(client=client).embed(["an NSAID with warfarin"])
    model, payload = calls[0]
    assert model == "amazon.titan-embed-text-v2:0"
    assert payload["dimensions"] == DIMENSIONS
    assert payload["normalize"] is True


def test_titan_refuses_a_response_of_the_wrong_width():
    """Silently storing a short vector would corrupt the column for every
    later search, and the failure would surface as bad retrieval rather than
    as an error."""
    client = _FakeBedrock([0.1] * 256)
    with pytest.raises(EmbedderError, match="not searchable"):
        TitanEmbedder(client=client).embed(["text"])


def test_titan_refuses_to_embed_nothing():
    """A zero vector sits at maximum distance from everything and quietly
    never matches."""
    with pytest.raises(EmbedderError, match="empty text"):
        TitanEmbedder(client=_FakeBedrock([0.1] * DIMENSIONS)).embed(["   "])


def test_the_model_is_pinned_rather_than_latest():
    """A different embedder is a different vector space, and a corpus half
    embedded by two models is not searchable — the same reason a prompt set
    carries a version."""
    assert embeddings.TITAN_MODEL.endswith("v2:0")


def test_one_call_per_text():
    """Titan's invoke_model takes one input at a time; if that ever changes
    this test is the reminder that the loop can become a batch."""
    calls: list = []
    TitanEmbedder(client=_FakeBedrock([0.1] * DIMENSIONS, calls)).embed(["a", "b", "c"])
    assert len(calls) == 3
