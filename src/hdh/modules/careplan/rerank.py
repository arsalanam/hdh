"""Reordering retrieved chunks by how well they answer the query (#100).

An embedding is computed once, before any query exists, so it has to
represent a whole chunk in the abstract. A reranker reads the query and the
chunk *together* and scores that pair — which is why it is more accurate and
why it cannot be used to search: it costs a model call per candidate, so it
can only reorder a shortlist something cheaper produced.

Hence `vector+rerank` rather than `rerank`: pgvector narrows the corpus to a
handful, the cross-encoder decides their order.

**What this is for, concretely.** Traceability governs 21 of 24 verdicts on
the cohort while *zero* elements are uncited — plans fail on citations that
are retrieved but do not support the claim. Vector search fixed the worst of
that (measured: lexical put the wrong document first on "patient bleeds
easily on blood thinners", vector put the right one first at 16× the score).
What it cannot fix is ordering *within* a set of plausible chunks, because
the vector never saw the question. That is the gap this closes.

**The fake is deterministic, not clever.** `LexicalReranker` scores on word
overlap. It is enough to prove the pipeline reorders, and it makes no claim
about quality — the same discipline as `HashingEmbedder`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import Protocol

#: Which reranker, and how many candidates to hand it.
ENV_VAR = "HDH_CAREPLAN_RERANKER"
DEFAULT = "cohere"

#: How many chunks the vector arm fetches before reranking.
#:
#: Reranking is the accurate half and the expensive half — one model call
#: per candidate — so the shortlist has to be wide enough to contain the
#: right answer and narrow enough to be worth paying for. Four times the
#: requested k gives the reranker room to promote something the embedding
#: ranked fourth, without turning a five-hit query into fifty calls.
CANDIDATE_MULTIPLIER = 4

#: Pinned, for the same reason the embedder is: a different reranker orders
#: differently, and a measurement taken under one does not carry to another.
COHERE_MODEL = "cohere.rerank-v3-5:0"


class RerankError(RuntimeError):
    """The reranker is unknown, unavailable, or answered unusably."""


class Reranker(Protocol):
    """Scores (query, passage) pairs and returns them best-first."""

    name: str

    def rerank(self, query: str, passages: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        """``(index into passages, score)``, most relevant first."""
        ...


class LexicalReranker:
    """Word-overlap scoring. For tests, and only for tests.

    Deterministic and explainable, so a test can assert an exact order. It
    is the same kind of matching the lexical retriever already does, which
    means it demonstrably cannot stand in for a cross-encoder — that is the
    point of using it only where quality is not the thing under test.
    """

    name = "lexical"

    def rerank(self, query: str, passages: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        """Score by shared word count, normalised by query length."""
        terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 2}
        scored = []
        for index, passage in enumerate(passages):
            words = {t for t in re.findall(r"\w+", passage.lower()) if len(t) > 2}
            overlap = len(terms & words) / (len(terms) or 1)
            scored.append((index, overlap))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_n]


class CohereReranker:
    """Cohere Rerank v3.5, through Bedrock's ``rerank`` API.

    Reached via ``bedrock-agent-runtime`` rather than ``bedrock-runtime``:
    reranking is not an ``invoke_model`` call, and passages are sent inline
    rather than from a knowledge base.
    """

    name = "cohere"

    def __init__(self, model: str = COHERE_MODEL, client=None, region: str | None = None) -> None:
        self.model = model
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise RerankError("reranking needs the bedrock extra: pip install 'hdh[bedrock]'") from None
            session = boto3.Session()
            self._region = self._region or session.region_name
            self._client = session.client("bedrock-agent-runtime")
        return self._client

    def _model_arn(self) -> str:
        region = self._region or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        return f"arn:aws:bedrock:{region}::foundation-model/{self.model}"

    def rerank(self, query: str, passages: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        """Score each passage against the query; best first.

        Raises:
            RerankError: the service answered with an index that is not in
                the passages it was given — which would silently attach a
                score to the wrong chunk and cite the wrong document.
        """
        if not passages:
            return []
        client = self.client
        response = client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": passage}},
                }
                for passage in passages
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._model_arn()},
                    "numberOfResults": min(top_n, len(passages)),
                },
            },
        )
        ordered = []
        for result in response.get("results", []):
            index = int(result["index"])
            if not 0 <= index < len(passages):
                raise RerankError(
                    f"{self.model} returned index {index} for {len(passages)} passages — "
                    f"a score attached to the wrong chunk would cite the wrong document"
                )
            ordered.append((index, float(result["relevanceScore"])))
        return ordered


_REGISTRY: dict[str, Callable[[], Reranker]] = {
    "cohere": lambda: CohereReranker(),
    "lexical": lambda: LexicalReranker(),
}


def register(name: str, factory: Callable[[], Reranker]) -> None:
    """Add or replace a reranker."""
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def configured() -> str:
    return (os.environ.get(ENV_VAR) or DEFAULT).strip().lower()


def build_reranker(name: str | None = None) -> Reranker:
    """The reranker for this run, or a refusal naming what exists."""
    chosen = (name or configured()).strip().lower()
    factory = _REGISTRY.get(chosen)
    if factory is None:
        raise RerankError(f"unknown reranker {chosen!r} — set {ENV_VAR} to one of: {', '.join(available())}")
    return factory()
