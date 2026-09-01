"""Which retriever a run uses, and how another one gets added.

Design §15.1, resolved 2026-08-27: retrieval strategy is **configuration**,
not a hardcoded choice. Three modes are planned —

    lexical          PostgreSQL full-text with a trigram fallback   (ships)
    vector           pgvector over API embeddings                   (#100)
    vector+rerank    the above, then a cross-encoder rerank         (#100)

— and `lexical` is the shipped default because it is the one that exists and
the one every current measurement was taken against.

**Why a factory rather than an import.** ``PgStore`` was constructed directly
in four places, which meant "configurable retrieval" was four edits away
rather than one. The :class:`~hdh.modules.careplan.knowledge.KnowledgeStore`
protocol always allowed a second implementation; nothing *selected* between
them. This is that seam, and it is deliberately a registry rather than an
if-else so a retriever can be added from outside this file — a specialty
module, an experiment, or a test double — without touching it.

**Unimplemented modes are registered, not omitted.** Asking for ``vector``
today fails with a message that says it is not built yet and points at the
issue, which is more useful than *"unknown retriever: vector"* and keeps the
roadmap visible where somebody configuring the module will actually read it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

#: Environment variable naming the retriever. Follows ``HDH_AGENT_MODEL``.
ENV_VAR = "HDH_CAREPLAN_RETRIEVER"

#: What a run uses when nothing says otherwise.
#:
#: Lexical rather than "no default": a module that refuses to run until it is
#: configured is a module nobody evaluates. The choice is still explicit —
#: it is written here, in one place, and every measurement in the eval
#: baseline was taken against it.
DEFAULT = "lexical"


class RetrieverError(RuntimeError):
    """The configured retriever cannot be built."""


#: name -> (factory, one-line description). A factory of ``None`` means the
#: mode is planned and not built.
_REGISTRY: dict[str, tuple[Callable[..., object] | None, str]] = {}


def register(name: str, factory: Callable[..., object] | None, description: str) -> None:
    """Add or replace a retriever.

    Public so a retriever can arrive from another module. Replacement is
    allowed on purpose — an experiment that wants to shadow ``lexical`` for
    one run should not have to rename itself to do it.
    """
    _REGISTRY[name] = (factory, description)


def available() -> list[str]:
    """Retrievers that can actually be built, in registration order."""
    return [name for name, (factory, _text) in _REGISTRY.items() if factory is not None]


def catalogue() -> Mapping[str, tuple[bool, str]]:
    """Every known retriever: name -> (is it built, what it is)."""
    return {name: (factory is not None, text) for name, (factory, text) in _REGISTRY.items()}


def configured() -> str:
    """The retriever this environment asks for."""
    return (os.environ.get(ENV_VAR) or DEFAULT).strip().lower()


def build_store(session, name: str | None = None):
    """The retriever for this run.

    Raises:
        RetrieverError: the name is unknown, or names a mode that is planned
            but not implemented. Both messages list what *is* available,
            because the caller is configuring something and needs the menu.
    """
    chosen = (name or configured()).strip().lower()
    entry = _REGISTRY.get(chosen)
    if entry is None:
        raise RetrieverError(
            f"unknown retriever {chosen!r} — set {ENV_VAR} to one of: {', '.join(available())}"
        )
    factory, description = entry
    if factory is None:
        # The description carries the pointer, rather than a hardcoded issue
        # number. #100 was hardcoded here until #100 was the issue that
        # BUILT these retrievers, at which point the refusal cited the work
        # that made it unnecessary.
        raise RetrieverError(
            f"retriever {chosen!r} is planned but not implemented yet ({description}). "
            f"Available now: {', '.join(available())}"
        )
    return factory(session)


def _register_bundled() -> None:
    from hdh.modules.careplan.knowledge import PgStore, RerankedVectorStore, VectorStore

    register("lexical", PgStore, "PostgreSQL full-text with a trigram fallback")
    register("vector", VectorStore, "pgvector over Titan embeddings (#100)")
    register("vector+rerank", RerankedVectorStore, "pgvector, then a Cohere cross-encoder rerank")


_register_bundled()
