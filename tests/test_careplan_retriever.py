"""Choosing a retriever, and adding one.

Design §15.1, resolved 2026-08-27: retrieval strategy is configuration
rather than a hardcoded choice, with `lexical` shipped as the default. The
`KnowledgeStore` protocol always allowed a second implementation; nothing
selected between them, and `PgStore` was constructed directly in four
places — so "configurable retrieval" was four edits away rather than one.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import retriever


@pytest.fixture(autouse=True)
def _restore_registry():
    """Registration is global, so a test that adds one must not leak it."""
    saved = dict(retriever._REGISTRY)  # noqa: SLF001 — the fixture owns this
    yield
    retriever._REGISTRY.clear()  # noqa: SLF001
    retriever._REGISTRY.update(saved)  # noqa: SLF001


# ── the default ──────────────────────────────────────────────────────────


def test_lexical_is_the_default(monkeypatch):
    """Lexical rather than "no default": a module that refuses to run until
    it is configured is a module nobody evaluates. The choice is explicit —
    written in one place — and every eval-baseline number was measured
    against it."""
    monkeypatch.delenv(retriever.ENV_VAR, raising=False)
    assert retriever.configured() == "lexical"
    assert retriever.DEFAULT == "lexical"


def test_the_environment_overrides_the_default(monkeypatch):
    monkeypatch.setenv(retriever.ENV_VAR, "vector")
    assert retriever.configured() == "vector"


def test_the_name_is_normalised(monkeypatch):
    """A config value with stray case or whitespace is a typo, not a
    different retriever."""
    monkeypatch.setenv(retriever.ENV_VAR, "  LEXICAL \n")
    assert retriever.configured() == "lexical"


def test_an_empty_setting_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setenv(retriever.ENV_VAR, "")
    assert retriever.configured() == "lexical"


# ── building ─────────────────────────────────────────────────────────────


def test_the_default_builds_the_lexical_store(monkeypatch):
    from hdh.modules.careplan.knowledge import PgStore

    monkeypatch.delenv(retriever.ENV_VAR, raising=False)
    assert isinstance(retriever.build_store(session=object()), PgStore)


def test_an_explicit_name_beats_the_environment(monkeypatch):
    monkeypatch.setenv(retriever.ENV_VAR, "vector")
    built = retriever.build_store(object(), "lexical")
    assert built.name == "postgres"


def test_an_unknown_retriever_lists_what_is_available():
    """The caller is configuring something and needs the menu, not just a
    refusal."""
    with pytest.raises(retriever.RetrieverError) as err:
        retriever.build_store(object(), "telepathy")
    assert "lexical" in str(err.value)
    assert retriever.ENV_VAR in str(err.value)


def test_a_planned_but_unbuilt_retriever_says_so_and_points_somewhere():
    """Registered rather than omitted, so the roadmap is visible where
    somebody configuring the module will read it — and so the failure is
    "not built yet" rather than "unknown".

    Every registered retriever is now built (#100), so this exercises the
    mechanism against a temporary entry. The path stays tested because the
    *next* planned retriever will use it, and a refusal nobody exercises is
    a refusal nobody has read.
    """
    retriever.register("hybrid", None, "planned: lexical and vector merged (#nnn)")
    try:
        with pytest.raises(retriever.RetrieverError) as err:
            retriever.build_store(object(), "hybrid")
        message = str(err.value)
        assert "not implemented" in message
        assert "#nnn" in message
        assert "lexical" in message
    finally:
        retriever._REGISTRY.pop("hybrid", None)


# ── the registry is the point ────────────────────────────────────────────


def test_a_retriever_can_be_added_from_outside_this_module():
    """The whole reason for a registry rather than an if-else: a specialty
    module, an experiment, or a test double should not need this file
    edited to exist."""

    class Fake:
        name = "fake"

        def __init__(self, session):
            self.session = session

    retriever.register("fake", Fake, "a test double")
    assert "fake" in retriever.available()
    assert isinstance(retriever.build_store(object(), "fake"), Fake)


def test_a_registration_can_shadow_a_shipped_one():
    """Replacement is allowed on purpose — an experiment that wants to
    stand in for `lexical` for one run should not have to rename itself."""

    class Fake:
        def __init__(self, session):
            pass

    retriever.register("lexical", Fake, "shadowed")
    assert isinstance(retriever.build_store(object(), "lexical"), Fake)


def test_available_lists_only_what_can_be_built():
    """#100 built both, so `available` and `catalogue` now agree. The
    distinction is still worth keeping: it is what lets a retriever be
    announced before it exists."""
    assert retriever.available() == ["lexical", "vector", "vector+rerank"]
    assert set(retriever.catalogue()) == {"lexical", "vector", "vector+rerank"}


def test_the_catalogue_says_which_are_built():
    catalogue = retriever.catalogue()
    assert all(built for built, _description in catalogue.values())
    assert all(description for _built, description in catalogue.values())


# ── nothing constructs a store directly any more ─────────────────────────


def test_no_caller_bypasses_the_factory():
    """The reason this refactor happened. A direct `PgStore(session)` is a
    place configuration silently does not reach — including ingestion,
    where a vector retriever would need embeddings written and a corpus
    loaded by one store and searched by another returns nothing while
    looking fine.
    """
    import pathlib

    module = pathlib.Path(retriever.__file__).parent
    offenders = []
    for path in module.rglob("*.py"):
        if path.name in {"retriever.py", "knowledge.py"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "PgStore(" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, "construct stores through build_store(): " + ", ".join(offenders)
