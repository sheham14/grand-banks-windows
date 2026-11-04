"""The web API and meteogram page (build step 8).

Three addresses:

    GET /                            the meteogram page (api/static/meteogram.html)
    GET /api/sites                   every site in config/sites.yaml
    GET /api/sites/{site_id}/latest  each model's newest good run at that site

FastAPI also writes interactive docs for the two /api addresses at /docs.

Run it:
    docker compose up -d db api                          (laptop or VM)
    uvicorn api.main:app --reload                        (laptop, while editing)
Then open http://localhost:8000. On the VM it listens on the VM's own
localhost only, so from the laptop open an SSH tunnel first:
    ssh -L 8000:localhost:8000 gbw-vm

What "latest" means: for EACH model separately, the newest run that
  - was extracted (extracted_at is set),
  - wasn't refused (extract_error is empty: e.g. directions that
    disagreed with ECCC's WDIR), and
  - has values for this site (a site added later only has values in
    the runs extracted after it was added).
Per model, because the models don't arrive together: HRDPS lands about
35 minutes before RDWPS, so for that half hour the newest wind comes
from a newer run than the newest waves. The page labels each panel with
its own run.

Values come back exactly as stored: SI units, each with its unit. The
page converts wind to knots for display (the project rule: store and
serve SI, convert only for display). Phase 3's window engine will use
this same data in SI.

The AWIPS counterpart is EDEX's request server: clients ask the server
for data instead of reading its database themselves. (A simplification:
EDEX speaks Thrift, not HTTP and JSON.)
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from ingest.db import connect
from ingest.models import load_models
from ingest.sitegrid import load_sites

STATIC = Path(__file__).resolve().parent / "static"

# A run older than this is flagged stale, and the page shows a warning.
# Runs start every 6 hours and are extracted about 3.5 hours after their
# run time, so in normal operation the newest run is 3.5 to 9.5 hours
# old. Past 12 hours, at least one run is overdue: the pipeline may have
# stopped, and old forecasts must not look current.
STALE_AFTER = timedelta(hours=12)

# Read once at startup. Editing a config file means restarting the
# service (docker compose up -d --build api), as for the other services.
SITES = {s.id: s for s in load_sites()}
MODELS = load_models()

app = FastAPI(
    title="Grand Banks Weather Window Finder",
    description="Point forecasts from ECCC's HRDPS and RDWPS. "
    "Prototype, not for operational decisions.",
)


def get_conn():
    """One read-only database connection per request.

    FastAPI calls this for every endpoint that asks for it (the
    Depends(get_conn) below), and closes the connection when the request
    is done. The tests swap it for their own throwaway connection.
    """
    with connect(read_only=True) as conn:
        yield conn


@app.get("/", include_in_schema=False)
def page() -> FileResponse:
    """The meteogram page. All of its data comes from the /api addresses."""
    return FileResponse(STATIC / "meteogram.html")


@app.get("/api/sites")
def sites() -> list[dict]:
    """Every site we forecast for, in config order, with its role
    ("forecast" or "verification")."""
    return [
        {"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon, "role": s.role}
        for s in SITES.values()
    ]


@app.get("/api/sites/{site_id}/latest")
def latest(site_id: str, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """Each model's newest good run at one site, with all its values.

    "models" has one entry per model in config/models.yaml. An entry is
    null when that model has no usable run at this site yet.
    """
    site = SITES.get(site_id)
    if site is None:
        raise HTTPException(404, f"unknown site {site_id!r}; known sites: {', '.join(SITES)}")

    now = datetime.now(timezone.utc)
    return {
        "site": {"id": site.id, "name": site.name, "lat": site.lat, "lon": site.lon,
                 "role": site.role},
        "generated_at": _iso(now),
        "models": {name: _latest_run(conn, name, site_id, now) for name in MODELS},
    }


def freshness(run_time: datetime, now: datetime) -> tuple[float, bool]:
    """(age in hours, stale?) of a run at time `now`.

    Age is counted from the run time (the model's start time), not from
    when we received it: that's what tells a reader how old the forecast
    really is. Exactly STALE_AFTER old is not yet stale.
    """
    age = now - run_time
    return round(age.total_seconds() / 3600, 1), age > STALE_AFTER


def _latest_run(conn: psycopg.Connection, model: str, site_id: str, now: datetime) -> dict | None:
    """The newest good run of one model at one site, or None."""
    run = conn.execute(
        """
        SELECT r.id, r.run_time, r.extracted_at
        FROM runs r
        WHERE r.model = %s
          AND r.extracted_at IS NOT NULL
          AND r.extract_error IS NULL
          AND EXISTS (SELECT 1 FROM point_forecasts p
                      WHERE p.run_id = r.id AND p.site_id = %s)
        ORDER BY r.run_time DESC
        LIMIT 1
        """,
        (model, site_id),
    ).fetchone()
    if run is None:
        return None
    run_id, run_time, extracted_at = run

    # member IS NULL: the deterministic forecast. Ensemble members
    # (Phase 3) will get their own address.
    rows = conn.execute(
        """
        SELECT variable, lead_hours, valid_time, value, unit
        FROM point_forecasts
        WHERE run_id = %s AND site_id = %s AND member IS NULL
        ORDER BY variable, lead_hours
        """,
        (run_id, site_id),
    ).fetchall()

    # Group by variable: {"wind_speed": {"unit": "m/s", "values": [...]}}.
    # One unit per variable is stated once, and checked: if a variable
    # ever came back in two units, mixing them on one chart would be
    # silently wrong, so we fail loudly instead.
    variables: dict[str, dict] = {}
    for variable, lead, valid_time, value, unit in rows:
        v = variables.setdefault(variable, {"unit": unit, "values": []})
        if v["unit"] != unit:
            raise RuntimeError(
                f"{model} run {run_id}, {site_id}: {variable} stored in both "
                f"{v['unit']!r} and {unit!r}"
            )
        v["values"].append({"lead_hours": lead, "valid_time": _iso(valid_time), "value": value})

    age_hours, stale = freshness(run_time, now)
    return {
        "description": MODELS[model].description,
        "run_time": _iso(run_time),
        "extracted_at": _iso(extracted_at),
        "age_hours": age_hours,
        "stale": stale,
        "variables": variables,
    }


def _iso(t: datetime) -> str:
    """UTC time as ISO 8601 with a Z, e.g. "2026-11-03T18:00:00Z".

    The database hands back timezone-aware times; converting to UTC here
    makes the output UTC whatever the database session's time zone is.
    """
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
