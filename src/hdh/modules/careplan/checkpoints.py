"""Where a plan run's state is kept between invocations.

Stages 2 and 3 of `careplan-state-and-graph.md`, merged: adopting the graph
without a checkpointer buys nothing, because re-entry itself needs one —
``update_state`` requires a thread, and a thread requires somewhere to keep
it. So the separable axis is not graph-versus-no-graph, it is
**memory-versus-durable**, and that is what this chooses between.

    memory      in-process, gone when the process ends   (tests, one-shot runs)
    postgres    durable, in the same database as the chart  (default)

A registry rather than an if-else, for the same reason
:mod:`~hdh.modules.careplan.retriever` is one: a saver should be addable
from outside this file, and a test double should not require editing the
selection logic.

**The default is durable.** A checkpointer that forgets is indistinguishable
from no checkpointer for everything the design wants it for — resume after a
crash, a clinician returning tomorrow, review that outlives the process.
Choosing memory silently would make those features look broken rather than
absent.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

#: Environment variable naming the checkpointer.
ENV_VAR = "HDH_CAREPLAN_CHECKPOINTS"

#: Durable unless told otherwise — see the module docstring.
DEFAULT = "postgres"


#: Our own types that a checkpoint may legitimately contain.
#:
#: LangGraph warns on deserialising types it was not told about and says it
#: **will block them in a future version** — so an unlisted type is not a
#: warning to live with, it is a run that stops working on an upgrade. Listing
#: them is also a security boundary worth having: a checkpoint is data, and
#: reconstructing arbitrary classes from data is how deserialisation bugs
#: start.
ALLOWED_MODULES: tuple[tuple[str, str], ...] = (
    ("hdh.modules.careplan.context", "CarePlanContext"),
    ("hdh.modules.careplan.context", "ProblemView"),
    ("hdh.modules.careplan.context", "MedicationView"),
    ("hdh.modules.careplan.context", "SocialView"),
    ("hdh.modules.careplan.stratify", "RiskFlag"),
    ("hdh.modules.careplan.triage", "Topic"),
    ("hdh.modules.careplan.generate", "ConcernDraft"),
    ("hdh.modules.careplan.generate", "GoalDraft"),
    ("hdh.modules.careplan.generate", "InterventionDraft"),
    ("hdh.modules.careplan.reconcile", "ReconcileReport"),
)


def serializer():
    """A serialiser that knows which of our types a checkpoint may hold."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODULES)


class CheckpointError(RuntimeError):
    """The configured checkpointer cannot be built."""


#: name -> (factory taking a session, description)
_REGISTRY: dict[str, tuple[Callable[..., object], str]] = {}


def register(name: str, factory: Callable[..., object], description: str) -> None:
    """Add or replace a checkpointer."""
    _REGISTRY[name] = (factory, description)


def available() -> list[str]:
    """Checkpointers that can be built, in registration order."""
    return list(_REGISTRY)


def catalogue() -> Mapping[str, str]:
    """Every checkpointer: name -> what it is."""
    return {name: text for name, (_factory, text) in _REGISTRY.items()}


def configured() -> str:
    """The checkpointer this environment asks for."""
    return (os.environ.get(ENV_VAR) or DEFAULT).strip().lower()


def _memory(_session=None):
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver(serde=serializer())


def _postgres(session=None):
    """A saver on the chart's own database.

    Deliberately the same database: a checkpoint that cannot be joined
    against the plan it belongs to is a second store with its own lifecycle,
    which is the argument §6 already made against a separate knowledge file.

    It opens its **own** connection rather than borrowing the session's. The
    saver commits on its own schedule — that is the point of a checkpoint —
    and sharing a transaction with the plan write would mean a rolled-back
    plan taking its checkpoints with it.
    """
    if session is None:
        raise CheckpointError("the postgres checkpointer needs a session to find its database")

    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    url = session.get_bind().url
    if url.get_backend_name() != "postgresql":
        raise CheckpointError(
            f"durable checkpoints need PostgreSQL, and this session is on "
            f"{url.get_backend_name()} — set {ENV_VAR}=memory for a non-durable run"
        )
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    # `dict_row` is not a preference: the saver reads its rows by name.
    connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    saver = PostgresSaver(connection, serde=serializer())
    # Creates the checkpoint tables if they are absent. Idempotent, and kept
    # out of Alembic on purpose: they are LangGraph's schema, versioned with
    # LangGraph, and a migration of ours would fight its upgrades.
    saver.setup()
    return saver


def build_checkpointer(session=None, name: str | None = None):
    """The checkpointer for this run.

    Raises:
        CheckpointError: unknown name, or a durable saver asked for without
            the database to put it in.
    """
    chosen = (name or configured()).strip().lower()
    entry = _REGISTRY.get(chosen)
    if entry is None:
        raise CheckpointError(
            f"unknown checkpointer {chosen!r} — set {ENV_VAR} to one of: {', '.join(available())}"
        )
    factory, _description = entry
    return factory(session)


register("postgres", _postgres, "durable, in the same database as the chart")
register("memory", _memory, "in-process; forgotten when the process ends")
