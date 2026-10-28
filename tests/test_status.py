"""Tests for ingest/status.py.

decide_status and expected_run_times are pure, so they're tested
directly. update_statuses uses the throwaway gbw_test database (the
`conn` fixture in conftest.py), with real filenames and publish times
from 2026-10-28.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

from ingest.db import get_or_create_run, record_file
from ingest.models import Variable, load_models
from ingest.status import (
    COMPLETE,
    INCOMPLETE,
    LATE,
    RECEIVING,
    decide_status,
    expected_run_times,
    update_statuses,
)

HRDPS = load_models()["HRDPS"]
LATE_AFTER = timedelta(hours=4)
GIVE_UP = timedelta(hours=8)
RUN_12Z = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def status_at(missing, hours_after_run, minutes=0):
    now = RUN_12Z + timedelta(hours=hours_after_run, minutes=minutes)
    return decide_status(missing, RUN_12Z, now, LATE_AFTER, GIVE_UP)


# --- decide_status: the edge cases -------------------------------------

def test_still_arriving():
    assert status_at(missing=100, hours_after_run=3) == RECEIVING


def test_one_minute_before_late_deadline():
    assert status_at(missing=1, hours_after_run=3, minutes=59) == RECEIVING


def test_exactly_at_late_deadline_is_late():
    assert status_at(missing=1, hours_after_run=4) == LATE


def test_exactly_at_give_up_deadline_is_incomplete():
    assert status_at(missing=1, hours_after_run=8) == INCOMPLETE


def test_complete_wins_even_when_old():
    # A run the fallback filled after the deadlines is complete, not late.
    assert status_at(missing=0, hours_after_run=20) == COMPLETE


# --- expected_run_times ------------------------------------------------

def test_run_times_include_both_ends():
    assert expected_run_times(HRDPS, utc(2026, 10, 28, 6), utc(2026, 10, 28, 12)) == [
        utc(2026, 10, 28, 6), utc(2026, 10, 28, 12)]


def test_run_times_cross_midnight():
    assert expected_run_times(HRDPS, utc(2026, 10, 28, 13), utc(2026, 10, 29, 7)) == [
        utc(2026, 10, 28, 18), utc(2026, 10, 29, 0), utc(2026, 10, 29, 6)]


def test_no_run_times_in_an_empty_range():
    assert expected_run_times(HRDPS, utc(2026, 10, 28, 13), utc(2026, 10, 28, 17)) == []


# --- update_statuses, against the database -----------------------------

# A cut-down HRDPS config (1 variable x 3 lead hours = 3 files), so a run
# can be made complete with three real files.
SMALL = dataclasses.replace(
    HRDPS, variables=(Variable("WIND", "AGL-10m"),), lead_hours=(0, 24, 48)
)
WIND_12Z = {
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2": utc(2026, 10, 28, 14, 51, 30),
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT024H.grib2": utc(2026, 10, 28, 15, 7, 11),
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT048H.grib2": utc(2026, 10, 28, 15, 17, 14),
}
WIND_06Z_000 = "20261028T06Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2"


def run_status(conn, run_time):
    return conn.execute(
        "SELECT status FROM runs WHERE model = 'HRDPS' AND run_time = %s", (run_time,)
    ).fetchone()


def test_full_run_becomes_complete(conn):
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, SMALL.files_per_run)
    for name, t in WIND_12Z.items():
        record_file(conn, run_id, name, t)
    for_fallback = update_statuses(conn, SMALL, now=utc(2026, 10, 28, 15, 30))
    assert run_status(conn, RUN_12Z) == (COMPLETE,)
    assert for_fallback == []


def test_partial_run_past_deadline_goes_to_fallback(conn):
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, SMALL.files_per_run)
    first = next(iter(WIND_12Z))
    record_file(conn, run_id, first, WIND_12Z[first])
    for_fallback = update_statuses(conn, SMALL, now=utc(2026, 10, 28, 16, 30))
    assert run_status(conn, RUN_12Z) == (LATE,)
    assert for_fallback == [(RUN_12Z, frozenset(WIND_12Z) - {first})]


def test_run_that_never_arrived_gets_a_row(conn):
    # Only the 06Z run has a file. By 16:30Z the 12Z run is past its late
    # deadline (12Z + 4 h = 16Z) with no files at all: it must appear.
    run_06 = get_or_create_run(conn, "HRDPS", utc(2026, 10, 28, 6), SMALL.files_per_run)
    record_file(conn, run_06, WIND_06Z_000, utc(2026, 10, 28, 8, 47, 53))
    update_statuses(conn, SMALL, now=utc(2026, 10, 28, 16, 30))
    assert run_status(conn, RUN_12Z) == (LATE,)


def test_no_rows_before_the_first_recorded_run(conn):
    # The first ever run is 12Z; 00Z and 06Z that day came before the
    # system existed and must not be reported missing.
    run_id = get_or_create_run(conn, "HRDPS", RUN_12Z, SMALL.files_per_run)
    for name, t in WIND_12Z.items():
        record_file(conn, run_id, name, t)
    update_statuses(conn, SMALL, now=utc(2026, 10, 28, 17))
    count = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    assert count == 1
