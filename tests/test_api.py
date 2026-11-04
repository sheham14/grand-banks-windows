"""Tests for build step 8: the web API (api/main.py).

They use the throwaway gbw_test database from conftest.py (every test's
changes are rolled back), so they skip if Postgres isn't running. The API
is called through FastAPI's TestClient, which makes real HTTP-style
requests without starting a server.

The values are real, as the store job saved them on the laptop:
HRDPS and RDWPS 2026-10-28 18Z at Nickerson Bank, hours 0 and 1, and
HRDPS 2026-10-28 12Z hour 24 (notebooks/02-wind-rotation-check.ipynb).
Two runs with different numbers let a test tell which one came back.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import STALE_AFTER, app, freshness, get_conn
from ingest.db import (
    connect,
    get_or_create_run,
    mark_extract_failed,
    mark_raw_deleted,
    save_point_values,
    set_status,
)
from ingest.extract import PointValue
from ingest.sitegrid import load_sites
from ingest.wind import ms_to_knots

RUN_12Z = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
RUN_18Z = datetime(2026, 10, 28, 18, tzinfo=timezone.utc)

# (lead_hours, variable, value, unit)
HRDPS_12Z = [(24, "wind_speed", 11.06, "m/s"), (24, "wind_direction", 268.41, "degrees")]
HRDPS_18Z = [
    (0, "wind_speed", 11.732092737444589, "m/s"), (0, "wind_direction", 297.3131999668025, "degrees"),
    (1, "wind_speed", 11.284821664519116, "m/s"), (1, "wind_direction", 296.4400188697331, "degrees"),
]
RDWPS_18Z = [
    (0, "wave_height", 2.50586, "m"), (0, "wave_period_peak", 8.373321875, "s"),
    (1, "wave_height", 2.50293, "m"), (1, "wave_period_peak", 8.3287548828125, "s"),
]


@pytest.fixture
def client(conn):
    """A TestClient whose API reads from the test's own connection.

    dependency_overrides swaps get_conn (a real read-only connection) for
    the test's connection, so the API sees the rows the test just added,
    and they're all rolled back afterwards.
    """
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


def _store(conn, model, run_time, rows, site_id="nickerson_bank", status="complete"):
    """Record a finished run and store its values, as the store job does."""
    run_id = get_or_create_run(conn, model, run_time, 196 if model == "HRDPS" else 98)
    set_status(conn, run_id, status)
    save_point_values(conn, run_id, [
        PointValue(model=model, run_time=run_time, valid_time=run_time + timedelta(hours=lead),
                   lead_hours=lead, member=None, site_id=site_id, variable=variable,
                   value=value, unit=unit)
        for lead, variable, value, unit in rows
    ])
    return run_id


def _latest(client, site_id="nickerson_bank"):
    res = client.get(f"/api/sites/{site_id}/latest")
    assert res.status_code == 200, res.text
    return res.json()


# --- sites --------------------------------------------------------------------

def test_sites_lists_every_configured_site(client):
    body = client.get("/api/sites").json()
    assert [s["id"] for s in body] == [s.id for s in load_sites()]
    banquereau = next(s for s in body if s["id"] == "banquereau_bank")
    assert banquereau["role"] == "verification"


def test_unknown_site_is_404(client):
    res = client.get("/api/sites/hibernia/latest")
    assert res.status_code == 404
    assert "nickerson_bank" in res.json()["detail"]  # lists the known sites


# --- which run is "latest" ------------------------------------------------------

def test_latest_is_newest_run_with_values_in_si_units(client, conn):
    _store(conn, "HRDPS", RUN_12Z, HRDPS_12Z)
    _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z)
    _store(conn, "RDWPS", RUN_18Z, RDWPS_18Z)
    models = _latest(client)["models"]

    wind = models["HRDPS"]
    assert wind["run_time"] == "2026-10-28T18:00:00Z"
    speed = wind["variables"]["wind_speed"]
    assert speed["unit"] == "m/s"  # served as stored; only the page converts to knots
    assert [p["lead_hours"] for p in speed["values"]] == [0, 1]
    assert speed["values"][0] == {"lead_hours": 0, "valid_time": "2026-10-28T18:00:00Z",
                                  "value": 11.732092737444589}
    assert wind["variables"]["wind_direction"]["unit"] == "degrees"

    assert models["RDWPS"]["variables"]["wave_height"]["unit"] == "m"
    assert models["RDWPS"]["variables"]["wave_height"]["values"][1]["value"] == 2.50293


def test_refused_run_is_skipped(client, conn):
    # A refused run can still have rows: a run extracted while incomplete
    # keeps its values, and if the re-extraction once it's complete is
    # refused, extract_error is set on top of them. Those values were
    # never confirmed against WDIR, so they must not be shown.
    _store(conn, "HRDPS", RUN_12Z, HRDPS_12Z)
    refused = _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z)
    mark_extract_failed(conn, refused, "our direction vs ECCC's WDIR is 46 degrees apart")
    assert _latest(client)["models"]["HRDPS"]["run_time"] == "2026-10-28T12:00:00Z"


def test_run_waiting_for_re_extraction_is_skipped(client, conn):
    # An incomplete run is extracted with gaps. When its missing files
    # arrive, set_status("complete") clears extracted_at so the store job
    # extracts it again. Until then its old rows are half a forecast.
    _store(conn, "HRDPS", RUN_12Z, HRDPS_12Z)
    waiting = _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z, status="incomplete")
    set_status(conn, waiting, "complete")
    assert _latest(client)["models"]["HRDPS"]["run_time"] == "2026-10-28T12:00:00Z"


def test_run_whose_raw_files_were_deleted_is_still_served(client, conn):
    # The cleanup deletes GRIB files of old extracted runs. Their values
    # in point_forecasts are the forecast; losing the files must not
    # hide it.
    run_id = _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z)
    mark_raw_deleted(conn, run_id)
    assert _latest(client)["models"]["HRDPS"]["run_time"] == "2026-10-28T18:00:00Z"


def test_run_without_this_site_is_skipped(client, conn):
    # A newer run with values for another site only (e.g. extracted
    # before this site was added to sites.yaml) is not this site's latest.
    _store(conn, "HRDPS", RUN_12Z, HRDPS_12Z, site_id="nickerson_bank")
    _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z, site_id="wsp_grand_banks")
    assert _latest(client)["models"]["HRDPS"]["run_time"] == "2026-10-28T12:00:00Z"


def test_model_with_no_run_is_null(client, conn):
    _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z)
    assert _latest(client)["models"]["RDWPS"] is None


# --- freshness ----------------------------------------------------------------

def test_freshness_boundary():
    assert freshness(RUN_18Z, RUN_18Z + timedelta(hours=3, minutes=48)) == (3.8, False)
    # Exactly 12 hours is the last moment that's still fine...
    assert freshness(RUN_18Z, RUN_18Z + STALE_AFTER) == (12.0, False)
    # ...one second later, it's stale.
    assert freshness(RUN_18Z, RUN_18Z + STALE_AFTER + timedelta(seconds=1))[1] is True


def test_old_run_is_flagged_stale(client, conn):
    _store(conn, "HRDPS", RUN_18Z, HRDPS_18Z)
    wind = _latest(client)["models"]["HRDPS"]
    assert wind["stale"] is True
    assert wind["age_hours"] > 12


# --- safety ---------------------------------------------------------------------

def test_mixed_units_fail_loudly(client, conn):
    # The same variable in two units would draw nonsense on one chart.
    # Hour 1 here is the real hour-1 speed, but in knots.
    _store(conn, "HRDPS", RUN_18Z, [
        (0, "wind_speed", 11.732092737444589, "m/s"),
        (1, "wind_speed", ms_to_knots(11.284821664519116), "kt"),
    ])
    with pytest.raises(RuntimeError, match="both 'm/s' and 'kt'"):
        client.get("/api/sites/nickerson_bank/latest")


def test_read_only_connection_refuses_writes(test_db):
    # What the API runs on: Postgres itself refuses any change.
    with connect(test_db, read_only=True) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("UPDATE runs SET status = status")


def test_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "Prototype, not for operational decisions." in res.text
