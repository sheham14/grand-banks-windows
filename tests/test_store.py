"""Tests for step 7: the point_forecasts table, its db.py functions, and
the store job's choice of which run to extract.

They use the throwaway gbw_test database from conftest.py (every test's
changes are rolled back), so they skip if Postgres isn't running. The
values are the real Nickerson Bank numbers for HRDPS 2026-10-28 12Z,
hour 24 (notebooks/02-wind-rotation-check.ipynb).
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from ingest.db import (
    get_or_create_run,
    mark_extract_failed,
    next_run_to_extract,
    save_point_values,
    set_status,
)
from ingest.extract import PointValue, extract_run, locate_for_run
from ingest.models import file_name, load_models

RUN = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
MODELS = ["HRDPS", "RDWPS"]


def _values(model="HRDPS", run_time=RUN):
    """Two real values: wind speed and direction at hour 24."""
    common = dict(model=model, run_time=run_time, valid_time=run_time + timedelta(hours=24),
                  lead_hours=24, member=None, site_id="nickerson_bank")
    return [
        PointValue(variable="wind_speed", value=11.06, unit="m/s", **common),
        PointValue(variable="wind_direction", value=268.41, unit="degrees", **common),
    ]


def _run(conn, model="HRDPS", run_time=RUN, status="complete"):
    run_id = get_or_create_run(conn, model, run_time, 196 if model == "HRDPS" else 98)
    set_status(conn, run_id, status)
    return run_id


def _extracted_at(conn, run_id):
    return conn.execute("SELECT extracted_at FROM runs WHERE id = %s", (run_id,)).fetchone()[0]


# --- saving -----------------------------------------------------------------

def test_save_stores_values_and_marks_run(conn):
    run_id = _run(conn)
    assert save_point_values(conn, run_id, _values()) == 2
    assert _extracted_at(conn, run_id) is not None
    row = conn.execute(
        "SELECT value, unit, member FROM point_forecasts "
        "WHERE run_id = %s AND variable = 'wind_direction'", (run_id,)
    ).fetchone()
    assert row == (268.41, "degrees", None)


def test_saving_twice_stores_once(conn):
    # The NULL trap: member is NULL for HRDPS. Only NULLS NOT DISTINCT
    # makes the second save count as a duplicate.
    run_id = _run(conn)
    save_point_values(conn, run_id, _values())
    assert save_point_values(conn, run_id, _values()) == 2  # still 2, not 4


def test_plain_unique_lets_null_duplicates_in(conn):
    # Why NULLS NOT DISTINCT is there: Postgres's default, shown on a
    # throwaway table. Two rows identical except both "empty" both go in.
    conn.execute("CREATE TEMP TABLE demo (run_id int, member int, UNIQUE (run_id, member))")
    conn.execute("INSERT INTO demo VALUES (1, NULL)")
    conn.execute("INSERT INTO demo VALUES (1, NULL)")  # no error!
    assert conn.execute("SELECT count(*) FROM demo").fetchone()[0] == 2


def test_failure_halfway_saves_nothing(conn):
    # The second value breaks a NOT NULL rule. The first must not stay
    # behind, and the run must not be marked extracted.
    run_id = _run(conn)
    good, bad = _values()
    bad = dataclasses.replace(bad, unit=None)
    with pytest.raises(psycopg.errors.NotNullViolation):
        save_point_values(conn, run_id, [good, bad])
    assert conn.execute("SELECT count(*) FROM point_forecasts").fetchone()[0] == 0
    assert _extracted_at(conn, run_id) is None


def test_values_from_another_run_are_refused(conn):
    run_id = _run(conn)
    with pytest.raises(ValueError, match="can't be stored"):
        save_point_values(conn, run_id, _values(model="RDWPS"))


# --- choosing the next run --------------------------------------------------

def test_picks_newest_finished_unextracted_run(conn):
    _run(conn, run_time=RUN - timedelta(hours=6), status="complete")
    newest = _run(conn, run_time=RUN, status="complete")
    _run(conn, run_time=RUN + timedelta(hours=6), status="late")  # still being fetched
    assert next_run_to_extract(conn, MODELS)[0] == newest


def test_incomplete_runs_are_extracted_too(conn):
    run_id = _run(conn, status="incomplete")
    assert next_run_to_extract(conn, MODELS)[0] == run_id


def test_extracted_run_is_not_picked_again(conn):
    run_id = _run(conn)
    save_point_values(conn, run_id, _values())
    assert next_run_to_extract(conn, MODELS) is None


def test_refused_run_is_not_retried(conn):
    run_id = _run(conn)
    mark_extract_failed(conn, run_id, "ExtractError: test")
    assert next_run_to_extract(conn, MODELS) is None


def test_run_completing_later_is_extracted_again(conn):
    # Extracted with gaps while incomplete; the fallback then finds the
    # missing files. Becoming complete puts it back in the queue.
    run_id = _run(conn, status="incomplete")
    save_point_values(conn, run_id, _values())
    assert next_run_to_extract(conn, MODELS) is None
    set_status(conn, run_id, "complete")
    assert next_run_to_extract(conn, MODELS)[0] == run_id


# --- one real hour, file to table ------------------------------------------

def test_real_hour_from_file_to_table(conn):
    hrdps = load_models()["HRDPS"]
    var = next(v for v in hrdps.variables if v.name == "UGRD")
    if not (hrdps.folder / file_name(hrdps, var, RUN, 24)).exists():
        pytest.skip("real GRIB files not in data/raw/")

    run_id = _run(conn)
    values = extract_run(hrdps, RUN, locate_for_run(hrdps, RUN), [24])
    save_point_values(conn, run_id, values)

    value, valid_time = conn.execute(
        """
        SELECT value, valid_time FROM point_forecasts
        WHERE run_id = %s AND site_id = 'nickerson_bank' AND variable = 'wind_direction'
        """,
        (run_id,),
    ).fetchone()
    assert value == pytest.approx(268.4, abs=0.1)  # ECCC's WDIR
    assert valid_time == datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
