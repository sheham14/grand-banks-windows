"""Tests for ingest/db.py, against the real Postgres in Docker.

The tests never touch the real gbw database. At the start of a test
session they create a fresh gbw_test database and build the tables from
db/init/*.sql (which also checks those scripts still run). Each test then
runs inside a transaction that is rolled back (undone) at the end, so
tests can't see each other's rows.

Filenames and publish times are real: the HRDPS 12Z run of 2026-10-28,
times taken from the files' modification times (ECCC's, via timeCopy).
If Postgres isn't running, these tests skip.
"""

from datetime import datetime, timezone

import psycopg
import pytest

from ingest.db import REPO_ROOT, connect, get_or_create_run, received_files, record_file
from ingest.models import expected_files, load_models

TEST_DB = "gbw_test"

RUN_12Z = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
# Real files from that run, with ECCC's publish times (to the second).
H000 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2"
H024 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT024H.grib2"
H048 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT048H.grib2"
T000 = datetime(2026, 10, 28, 14, 51, 30, tzinfo=timezone.utc)
T024 = datetime(2026, 10, 28, 15, 7, 11, tzinfo=timezone.utc)
T048 = datetime(2026, 10, 28, 15, 17, 14, tzinfo=timezone.utc)


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


def test_same_run_gets_same_id(conn):
    first = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    again = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    assert first == again


def test_other_model_same_time_is_another_run(conn):
    # Model + run time identify a run, so RDWPS 12Z is not HRDPS 12Z.
    hrdps = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    rdwps = get_or_create_run(conn, "RDWPS", RUN_12Z, 98)
    assert hrdps != rdwps


def test_duplicate_notice_counts_once(conn):
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    assert record_file(conn, run_id, H024, T024) is True    # new
    assert record_file(conn, run_id, H024, T024) is False   # same notice again
    assert received_files(conn, run_id) == {H024}


def test_first_and_last_times_out_of_order(conn):
    # Handle the middle file, then the last, then the first: the run's
    # times must still come out as the true earliest and latest.
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    record_file(conn, run_id, H024, T024)
    record_file(conn, run_id, H048, T048)
    record_file(conn, run_id, H000, T000)
    first, last = conn.execute(
        "SELECT first_file_at, last_file_at FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (first, last) == (T000, T048)


def test_missing_is_expected_minus_received(conn):
    hrdps = load_models()["HRDPS"]
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, hrdps.files_per_run)
    record_file(conn, run_id, H000, T000)
    record_file(conn, run_id, H048, T048)
    missing = expected_files(hrdps, RUN_12Z) - received_files(conn, run_id)
    assert len(missing) == 196 - 2
    assert H000 not in missing and H024 in missing


def test_naive_time_is_rejected(conn):
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    with pytest.raises(ValueError):
        record_file(conn, run_id, H000, datetime(2026, 10, 28, 14, 51, 30))


def test_files_expected_must_be_positive(conn):
    # The CHECK (files_expected > 0) in 001_runs.sql.
    with pytest.raises(psycopg.errors.CheckViolation):
        get_or_create_run(conn, "HRDPS", RUN_12Z, 0)
