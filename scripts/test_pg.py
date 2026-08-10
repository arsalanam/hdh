#!/usr/bin/env python
"""Run the PostgreSQL integration tests against the `just deps` containers.

Sets HDH_PG_TEST_URL to the local-container test database (hdh_test — see
docker/pg-init.sql) and runs the pg test module. Cross-platform stand-in
for an inline env assignment, per the justfile's logic-lives-in-scripts
rule.
"""

import os
import sys

import pytest

DEFAULT_URL = "postgresql+psycopg://hdh:hdh@localhost:5433/hdh_test"


def main() -> int:
    """Set the test-database URL (if unset) and run tests/test_postgres.py."""
    os.environ.setdefault("HDH_PG_TEST_URL", DEFAULT_URL)
    return pytest.main(["tests/test_postgres.py", "-q", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
