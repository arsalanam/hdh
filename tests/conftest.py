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
