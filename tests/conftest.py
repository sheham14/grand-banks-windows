"""Shared test fixtures. pytest loads this file automatically, so any
test file can ask for `conn` by naming it as a test argument.

The database tests never touch the real gbw database. At the start of a
test session they create a fresh gbw_test database and build the tables
from db/init/*.sql (which also checks those scripts still run). Each test
then runs inside a transaction that is rolled back (undone) at the end,
so tests can't see each other's rows. If Postgres isn't running, any
test that uses `conn` skips.
"""

import psycopg
import pytest

from ingest.db import REPO_ROOT, connect

TEST_DB = "gbw_test"


@pytest.fixture(scope="session")
def test_db():
    """Create a fresh gbw_test database once per test session."""
    try:
        admin = connect()
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable: {e}")
    with admin:
        # TEST_DB is our own constant, so pasting it into the SQL is safe
        # (database names can't be passed as %s parameters).
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with connect(TEST_DB) as conn:
        for script in sorted((REPO_ROOT / "db" / "init").glob("*.sql")):
            conn.execute(script.read_text())
    return TEST_DB


@pytest.fixture
def conn(test_db):
    """A connection whose changes are all undone after the test."""
    with connect(test_db) as c:
        # force_rollback: undo everything at the end, even on success.
        # The transaction blocks inside record_file become "savepoints"
        # (transactions nested inside this one), so they're undone too.
        with c.transaction(force_rollback=True):
            yield c
