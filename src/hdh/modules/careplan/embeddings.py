"""Text to vectors, behind a registry (#100).

Semantic retrieval needs an embedder, and an embedder is a network call to a
paid service. Every other injectable thing in this module follows the same
shape for the same reason — `Selector`, `Grader`, the retriever registry —
so this one does too: a protocol, a registry, a real backend and a
deterministic fake.

**Why the fake is not a shortcut.** The whole test suite has to run in CI
with no AWS account, and a vector store whose behaviour is only observable
against Bedrock is a store nobody can test. The hashing embedder produces
stable vectors from text alone, so the *plumbing* — storage, dimensions,
ordering, the SQL — is verified offline and only the quality of the vectors
depends on the paid call.

**What the fake cannot tell you.** It has no idea that "bleeding risk" and
"haemorrhage" are related; it is deterministic noise. So a test using it
proves the pipeline works, and proves nothing about whether retrieval got
better. That question needs the real embedder and the cohort, which is why
the measurement is separate from the machinery.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Callable, Sequence
from typing import Protocol

#: Which embedder to use, and how wide its vectors are.
ENV_VAR = "HDH_CAREPLAN_EMBEDDER"
DEFAULT = "titan"

#: Titan v2 supports 256, 512 and 1024. 1024 is the default and the most
#: faithful; 256 is a quarter of the storage and measurably worse on short
#: clinical text. Fixed here rather than per-call because a column has one
#: width and a corpus embedded at two widths cannot be searched as one.
DIMENSIONS = 1024

#: The model. Pinned rather than "latest": a different embedder produces a
#: different vector space, and a corpus half-embedded by two models is not
#: searchable — the same reason a prompt set carries a version.
TITAN_MODEL = "amazon.titan-embed-text-v2:0"


class EmbedderError(RuntimeError):
    """The embedder is unknown, unavailable, or answered unusably."""


class Embedder(Protocol):
    """Turns text into vectors of :data:`DIMENSIONS` floats."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input, in the same order."""
        ...


class HashingEmbedder:
    """Deterministic vectors from a hash. For tests, and only for tests.

    Stable across processes and machines — SHA-256 of the text, expanded to
    the required width — so a test that ingests and searches gets the same
    answer everywhere. It carries no meaning at all, which is the point: it
    exercises the pipeline without pretending to be a model.
    """

    name = "hashing"

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Unit vectors derived from SHA-256 of each text, in input order.

        Same width and same normalisation as the real embedder, so the
        storage layer cannot tell them apart — and no meaning whatsoever,
        so a test cannot accidentally conclude retrieval works well.
        """
        vectors = []
        for text in texts:
            raw = b""
            counter = 0
            needed = self.dimensions * 4
            while len(raw) < needed:
                raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
                counter += 1
            floats = [struct.unpack("<I", raw[i : i + 4])[0] / 2**32 - 0.5 for i in range(0, needed, 4)]
            norm = sum(f * f for f in floats) ** 0.5 or 1.0
            vectors.append([f / norm for f in floats])
        return vectors


class TitanEmbedder:
    """Amazon Titan Text Embeddings v2, via Bedrock.

    Returns unit-normalised vectors, so cosine similarity is a dot product
    and pgvector's ``<=>`` distance is ``1 - similarity``.

    Batching is a loop rather than a batch call: Titan's invoke_model takes
    one input at a time. The corpus is small enough that this is a handful
    of calls at ingest and exactly one per query.
    """

    name = "titan"

    def __init__(self, dimensions: int = DIMENSIONS, model: str = TITAN_MODEL, client=None) -> None:
        self.dimensions = dimensions
        self.model = model
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise EmbedderError(
                    "titan embeddings need the bedrock extra: pip install 'hdh[bedrock]'"
                ) from None
            self._client = boto3.Session().client("bedrock-runtime")
        return self._client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One Bedrock call per text, returning unit vectors in input order.

        Raises:
            EmbedderError: the text is empty, or the model answered with a
                width other than the one this corpus is stored at — either
                would corrupt the column for every later search.
        """
        import json

        vectors = []
        for text in texts:
            if not (text or "").strip():
                # An empty string has no meaning to embed. Refusing beats a
                # zero vector, which would sit at maximum distance from
                # everything and quietly never match.
                raise EmbedderError("cannot embed empty text")
            response = self.client.invoke_model(
                modelId=self.model,
                body=json.dumps({"inputText": text, "dimensions": self.dimensions, "normalize": True}),
            )
            payload = json.loads(response["body"].read())
            vector = payload.get("embedding")
            if not vector or len(vector) != self.dimensions:
                raise EmbedderError(
                    f"{self.model} returned {len(vector or [])} dimensions, expected "
                    f"{self.dimensions} — a corpus embedded at two widths is not searchable"
                )
            vectors.append([float(v) for v in vector])
        return vectors


_REGISTRY: dict[str, Callable[[], Embedder]] = {
    "titan": lambda: TitanEmbedder(),
    "hashing": lambda: HashingEmbedder(),
}


def register(name: str, factory: Callable[[], Embedder]) -> None:
    """Add or replace an embedder."""
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def configured() -> str:
    return (os.environ.get(ENV_VAR) or DEFAULT).strip().lower()


def build_embedder(name: str | None = None) -> Embedder:
    """The embedder for this run, or a refusal naming what exists."""
    chosen = (name or configured()).strip().lower()
    factory = _REGISTRY.get(chosen)
    if factory is None:
        raise EmbedderError(
            f"unknown embedder {chosen!r} — set {ENV_VAR} to one of: {', '.join(available())}"
        )
    return factory()
