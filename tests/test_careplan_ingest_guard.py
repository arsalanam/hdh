"""Ingest must not silently leave the vector retriever with nothing.

`ingest` replaces a corpus wholesale and only writes embeddings when the
configured retriever needs them. Run it with `HDH_CAREPLAN_RETRIEVER` unset
— the default is `lexical` — and every vector is deleted.

Nothing fails. The corpus reports the same chunk count, lexical retrieval
keeps working, and `vector+rerank` matches nothing: `search` answers "no
chunks match", which reads exactly like a query the corpus cannot answer.
It happened on this repository and went unnoticed until a retrieval
comparison returned zero hits for a query the corpus plainly covers — and
`ingest_corpus`'s own docstring had predicted it in words:

    a corpus loaded by one store and searched by another would return
    nothing while looking fine
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import cli


class _Args:
    def __init__(self, **kw):
        self.corpus = kw.get("corpus")
        self.root = kw.get("root")
        self.force = kw.get("force", False)


def test_the_guard_refuses_when_embeddings_would_be_dropped(monkeypatch):
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {"med_safety": 10})
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {"med_safety": 10})

    class _Lexical:  # no `embedder` attribute — this is the whole test
        pass

    from hdh.modules.careplan import retriever

    monkeypatch.setattr(retriever, "build_store", lambda _s: _Lexical())
    monkeypatch.setattr(retriever, "configured", lambda: "lexical")

    with pytest.raises(SystemExit) as raised:
        cli._refuse_to_drop_embeddings(None, ["med_safety"], force=False)
    message = str(raised.value)
    assert "would delete embeddings" in message
    assert "med_safety (10 embedded)" in message
    assert "vector+rerank" in message, "the message has to carry the fix"
    assert "--force" in message, "and the way past it"


def test_an_embedding_retriever_is_allowed_through(monkeypatch):
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {"med_safety": 10})

    class _Vector:
        embedder = object()

    from hdh.modules.careplan import retriever

    monkeypatch.setattr(retriever, "build_store", lambda _s: _Vector())
    cli._refuse_to_drop_embeddings(None, ["med_safety"], force=False)


def test_the_store_is_asked_rather_than_the_retriever_name(monkeypatch):
    """A new embedding retriever must not be blocked by a hardcoded list
    nobody remembered to update."""
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {"med_safety": 10})

    class _SomeFutureStore:
        embedder = object()

    from hdh.modules.careplan import retriever

    monkeypatch.setattr(retriever, "build_store", lambda _s: _SomeFutureStore())
    monkeypatch.setattr(retriever, "configured", lambda: "something-invented-later")
    cli._refuse_to_drop_embeddings(None, ["med_safety"], force=False)


def test_force_gets_past_it(monkeypatch):
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {"med_safety": 10})
    cli._refuse_to_drop_embeddings(None, ["med_safety"], force=True)


def test_nothing_to_protect_is_not_an_obstacle(monkeypatch):
    """A first ingest, or a corpus that never had vectors."""
    monkeypatch.setattr(cli, "_embedded_counts", lambda _s, _n: {})
    cli._refuse_to_drop_embeddings(None, ["med_safety"], force=False)


def test_the_count_does_not_come_from_the_orm_metadata():
    """`embedding` is added by migration 0017 and lives OUTSIDE the schema
    registry, so `Base.metadata.tables['knowledge_chunks'].c` has no such
    column. The first version of this guard read the metadata, found nothing
    to protect, and let the ingest through — reproducing the silence it
    exists to break, on the run meant to prove it worked."""
    import inspect

    from hdh.core.models import Base

    table = Base.metadata.tables.get("knowledge_chunks")
    if table is not None:
        assert "embedding" not in table.c, "if this is now registered, simplify the guard"
    # Checked on behaviour, not prose: the docstring names the trap, so a
    # substring search for "Base.metadata" matches the explanation.
    body = inspect.getsource(cli._embedded_counts).split('"""')[-1]
    assert "Base.metadata" not in body, "reading the registry is what missed the column"
    assert "count(embedding)" in body, "it has to ask the database directly"
