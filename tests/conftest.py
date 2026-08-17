import os

import pytest

from hdh.core.generators import build_dataset
from hdh.core.models import get_engine, get_session

# Unit tests must never touch a real database by accident: a developer's
# .env (loaded by `just`) may set HDH_DB_URL, which get_engine() would
# otherwise pick up for the shared in-memory fixture. The PostgreSQL
# integration tests opt in explicitly via HDH_PG_TEST_URL instead.
os.environ.pop("HDH_DB_URL", None)


@pytest.fixture(scope="session")
def db_session():
    """A small in-memory dataset shared across tests (schema bootstrapped)."""
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(":memory:")
    session = get_session(engine)
    build_dataset(session, n_patients=8, years_of_history=2, verbose=False)
    yield session
    session.close()


@pytest.fixture(autouse=True)
def no_live_llm_calls(request, monkeypatch):
    """`just qa` must never spend money or need an API key.

    Same spirit as popping HDH_DB_URL above: a test must not reach a real
    service by accident. Every comprehension test injects `stub_extractor`,
    and the eval harness is driven from the CLI on demand — but nothing
    *enforced* that until now, so one careless test could silently start
    billing on every qa run (and fail in CI, which has no key).

    Constructing a real Anthropic client during a test now fails loudly
    with instructions. Tests that genuinely need the network opt in with
    `@pytest.mark.llm`, which is deselected by default (see pyproject).
    """
    if request.node.get_closest_marker("llm"):
        return
    try:
        import anthropic
    except ImportError:  # the [agent] extra isn't installed — nothing to block
        return

    def _blocked(*_args, **_kwargs):
        raise AssertionError(
            "This test constructed a live Anthropic client, so `just qa` would "
            "make a billable API call. Inject a stub instead — "
            "`stub_extractor(raw)` for comprehension, or the client= parameter — "
            "or mark the test @pytest.mark.llm to run it on demand."
        )

    monkeypatch.setattr(anthropic.Anthropic, "__init__", _blocked)
