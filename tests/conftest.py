import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.generators import build_dataset


@pytest.fixture(scope="session")
def db_session():
    """A small in-memory dataset shared across tests."""
    engine = get_engine(":memory:")
    session = get_session(engine)
    build_dataset(session, n_patients=8, years_of_history=2, verbose=False)
    yield session
    session.close()
