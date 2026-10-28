"""Tests for ingest/db.py, against the real Postgres in Docker.

They use the `conn` fixture from conftest.py: a throwaway gbw_test
database, with every test's changes rolled back.

Filenames and publish times are real: the HRDPS 12Z run of 2026-10-28,
times taken from the files' modification times (ECCC's, via timeCopy).
If Postgres isn't running, these tests skip.
"""

from datetime import datetime, timezone

import psycopg
import pytest

from ingest.db import get_or_create_run, received_files, recorded_names, record_file
from ingest.models import expected_files, load_models

RUN_12Z = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
# Real files from that run, with ECCC's publish times (to the second).
H000 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2"
H024 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT024H.grib2"
H048 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT048H.grib2"
T000 = datetime(2026, 10, 28, 14, 51, 30, tzinfo=timezone.utc)
T024 = datetime(2026, 10, 28, 15, 7, 11, tzinfo=timezone.utc)
T048 = datetime(2026, 10, 28, 15, 17, 14, tzinfo=timezone.utc)


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


def test_recorded_names_is_per_model(conn):
    hrdps = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    get_or_create_run(conn, "RDWPS", RUN_12Z, 98)
    record_file(conn, hrdps, H000, T000)
    assert recorded_names(conn, "HRDPS") == {H000}
    assert recorded_names(conn, "RDWPS") == frozenset()


def test_naive_time_is_rejected(conn):
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, 196)
    with pytest.raises(ValueError):
        record_file(conn, run_id, H000, datetime(2026, 10, 28, 14, 51, 30))


def test_files_expected_must_be_positive(conn):
    # The CHECK (files_expected > 0) in 001_runs.sql.
    with pytest.raises(psycopg.errors.CheckViolation):
        get_or_create_run(conn, "HRDPS", RUN_12Z, 0)
