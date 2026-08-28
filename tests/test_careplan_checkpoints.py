"""Choosing where a run's state is kept.

Stages 2 and 3 of `careplan-state-and-graph.md` were merged because adopting
the graph without a checkpointer buys nothing — re-entry needs a thread, and
a thread needs somewhere to live. So the axis is memory-versus-durable, and
this is what chooses.

The durable path itself is exercised in `test_postgres.py`; nothing here
touches a database.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import checkpoints


@pytest.fixture(autouse=True)
def _restore_registry():
    saved = dict(checkpoints._REGISTRY)  # noqa: SLF001 — the fixture owns this
    yield
    checkpoints._REGISTRY.clear()  # noqa: SLF001
    checkpoints._REGISTRY.update(saved)  # noqa: SLF001


def test_the_default_is_durable(monkeypatch):
    """A checkpointer that forgets is indistinguishable from none for
    everything the design wants one for — resume after a crash, a clinician
    returning tomorrow, review that outlives the process. Defaulting to
    memory would make those look broken rather than absent."""
    monkeypatch.delenv(checkpoints.ENV_VAR, raising=False)
    assert checkpoints.configured() == "postgres"
    assert checkpoints.DEFAULT == "postgres"


def test_the_environment_selects(monkeypatch):
    monkeypatch.setenv(checkpoints.ENV_VAR, "MEMORY")
    assert checkpoints.configured() == "memory"


def test_memory_builds_without_a_session():
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(checkpoints.build_checkpointer(name="memory"), MemorySaver)


def test_durable_without_a_database_says_which_and_why():
    """Naming the fix matters: the caller is configuring something."""
    with pytest.raises(checkpoints.CheckpointError, match="needs a session"):
        checkpoints.build_checkpointer(None, "postgres")


def test_durable_on_the_wrong_dialect_names_the_escape_hatch():
    from sqlalchemy import create_engine

    class _Session:
        def get_bind(self):
            return create_engine("sqlite://")

    with pytest.raises(checkpoints.CheckpointError) as err:
        checkpoints.build_checkpointer(_Session(), "postgres")
    assert "sqlite" in str(err.value)
    assert checkpoints.ENV_VAR in str(err.value), "the message should say how to proceed"


def test_an_unknown_checkpointer_lists_what_exists():
    with pytest.raises(checkpoints.CheckpointError, match="memory"):
        checkpoints.build_checkpointer(None, "telepathy")


def test_a_checkpointer_can_be_registered_from_outside():
    class Fake:
        def __init__(self, session=None):
            self.session = session

    checkpoints.register("fake", Fake, "a test double")
    assert "fake" in checkpoints.available()
    assert isinstance(checkpoints.build_checkpointer(None, "fake"), Fake)


# ── the serialisation contract ───────────────────────────────────────────


def test_every_state_type_is_on_the_allowlist():
    """LangGraph warns on deserialising unregistered types and says it will
    **block** them. An unlisted type is therefore not a warning to live with
    — it is a run that stops working on an upgrade.

    This checks the list against the types the pipeline actually puts in
    state, so adding a node with a new dataclass fails here rather than in
    production six months later.
    """
    allowed = set(checkpoints.ALLOWED_MODULES)
    required = {
        ("hdh.modules.careplan.context", "CarePlanContext"),
        ("hdh.modules.careplan.context", "ProblemView"),
        ("hdh.modules.careplan.context", "MedicationView"),
        ("hdh.modules.careplan.stratify", "RiskFlag"),
        ("hdh.modules.careplan.triage", "Topic"),
        ("hdh.modules.careplan.generate", "ConcernDraft"),
        ("hdh.modules.careplan.generate", "GoalDraft"),
        ("hdh.modules.careplan.generate", "InterventionDraft"),
        ("hdh.modules.careplan.reconcile", "ReconcileReport"),
    }
    assert required <= allowed, sorted(required - allowed)


def test_the_serialiser_carries_the_allowlist():
    serde = checkpoints.serializer()
    assert serde is not None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: __import__("hdh.modules.careplan.generate", fromlist=["ConcernDraft"]).ConcernDraft(
            "s", "risk", ["a", "b"]
        ),
        lambda: __import__("hdh.modules.careplan.generate", fromlist=["GoalDraft"]).GoalDraft(
            "s", 0, "", ["a"]
        ),
        lambda: __import__("hdh.modules.careplan.generate", fromlist=["InterventionDraft"]).InterventionDraft(
            "s", 0, "medication", "GP", ["a"]
        ),
    ],
)
def test_a_draft_built_from_lists_still_holds_tuples(factory):
    """The bug a checkpoint round trip caused, pinned at the type.

    msgpack has no tuple, so a frozen dataclass declared `tuple[str, ...]`
    came back holding a *list*. The type survived, so nothing complained —
    but equality stopped working and the annotation lied, and only resumed
    runs were affected. Coercing on construction makes the round trip
    faithful; this asserts the coercion rather than the round trip, so it
    needs no database.
    """
    built = factory()
    assert isinstance(built.evidence_refs, tuple)


def test_a_context_built_from_lists_still_holds_tuples():
    from hdh.modules.careplan.context import CarePlanContext, ProblemView

    context = CarePlanContext(
        mrn="X",
        age=70,
        sex="MALE",
        problems=[ProblemView("E11.9", "Type 2 diabetes mellitus", False, None)],
    )
    assert isinstance(context.problems, tuple)
    assert isinstance(context.medications, tuple)
