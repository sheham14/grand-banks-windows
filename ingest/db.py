"""Database access for the ingest job (build step 5, piece 3).

Connects the pieces we already have to the two tables in db/init/:

    runs       one row per model run, e.g. (HRDPS, 2026-10-28 12Z)
    run_files  one row per file received for a run

The ingest job uses three functions, once per downloaded file:

    run_id = get_or_create_run(conn, "HRDPS", run_time, 196)
    is_new = record_file(conn, run_id, file_name, published_at)
    have   = received_files(conn, run_id)
    missing = expected_files(...) - have      # from ingest/models.py

plus recorded_names(), which the scan loop uses to skip files it has
already handled.

Every function is safe to call twice with the same input. AMQP can
deliver the same notice twice, and a restarted job re-scans files it
already handled, so "do it again" must never double-count anything.
The UNIQUE constraints in the tables are what make that true.

SQL values are always passed as %s / %(name)s parameters, never pasted
into the SQL text with f-strings. psycopg sends them separately from the
query, so a strange filename can't change what the SQL does
(that attack is called SQL injection).
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def connect(dbname: str | None = None) -> psycopg.Connection:
    """Open a connection to our Postgres, using the settings in .env.

    dbname overrides POSTGRES_DB; the tests use it to work in a separate
    gbw_test database so they never touch real data.
    """
    # load_dotenv copies .env into os.environ, but never overwrites a
    # variable that is already set. Locally the values come from .env;
    # inside Docker (step 9) Compose sets them and those win.
    load_dotenv(REPO_ROOT / ".env")

    return psycopg.connect(
        # 127.0.0.1 and 5434 are how WSL reaches the db container
        # (see docker-compose.yml). Inside Compose this becomes host "db",
        # port 5432; we'll set that in step 9.
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5434")),
        dbname=dbname or os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        # autocommit: each single statement is saved as soon as it runs.
        # Where two statements must succeed or fail together, we wrap them
        # in `with conn.transaction():` (see record_file).
        autocommit=True,
    )


def get_or_create_run(
    conn: psycopg.Connection, model: str, run_time: datetime, files_expected: int
) -> int:
    """Return the id of the (model, run_time) run, creating it if needed.

    The first file of a run creates the row; every later file finds it.
    files_expected is only used when creating; an existing run keeps the
    value it started with.
    """
    _require_utc(run_time, "run_time")

    # One statement, not "SELECT, and INSERT if nothing found". With two
    # steps, two files arriving at the same moment could both see "no run
    # yet" and both try to create it. A single INSERT ... ON CONFLICT is
    # atomic: Postgres settles the race using UNIQUE (model, run_time).
    #
    # Why DO UPDATE and not DO NOTHING: DO NOTHING returns no row when
    # the run already exists, so RETURNING id would give us nothing.
    # "SET model = EXCLUDED.model" changes nothing (it sets model to the
    # value it already has) but makes Postgres return the existing row.
    row = conn.execute(
        """
        INSERT INTO runs (model, run_time, files_expected)
        VALUES (%s, %s, %s)
        ON CONFLICT (model, run_time) DO UPDATE SET model = EXCLUDED.model
        RETURNING id
        """,
        (model, run_time, files_expected),
    ).fetchone()
    return row[0]


def record_file(
    conn: psycopg.Connection, run_id: int, file_name: str, published_at: datetime
) -> bool:
    """Record one received file. Returns True if new, False if already seen.

    published_at is ECCC's publish time, read from the file's modification
    time (sr3's timeCopy puts it there). It also moves the run's
    first_file_at / last_file_at if this file is earlier / later.
    """
    _require_utc(published_at, "published_at")

    # The insert and the update belong together: a file counted in
    # run_files must also be reflected in the run's times. Inside this
    # block both are saved, or (on any error) neither is.
    with conn.transaction():
        cur = conn.execute(
            """
            INSERT INTO run_files (run_id, file_name, published_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (run_id, file_name) DO NOTHING
            """,
            (run_id, file_name, published_at),
        )
        # rowcount = how many rows the INSERT added: 1 for a new file,
        # 0 if UNIQUE (run_id, file_name) said we already have it.
        if cur.rowcount == 0:
            return False

        # LEAST / GREATEST rather than "set it if it's empty": the scan
        # may handle files out of order (e.g. hour 048 before hour 000),
        # so we keep whichever time is truly earliest / latest.
        # Postgres's LEAST and GREATEST skip NULLs, so for a run's first
        # file (both columns still NULL) they simply return published_at.
        conn.execute(
            """
            UPDATE runs
            SET first_file_at = LEAST(first_file_at, %(t)s),
                last_file_at  = GREATEST(last_file_at, %(t)s)
            WHERE id = %(id)s
            """,
            {"t": published_at, "id": run_id},
        )
    return True


def received_files(conn: psycopg.Connection, run_id: int) -> frozenset[str]:
    """The names of every file recorded for a run.

    A frozenset, like expected_files() in ingest/models.py, so the two
    can be subtracted: expected - received = missing.
    """
    rows = conn.execute(
        "SELECT file_name FROM run_files WHERE run_id = %s", (run_id,)
    ).fetchall()
    # Each row is a 1-item tuple like ("2026...grib2",); (name,) unpacks it.
    return frozenset(name for (name,) in rows)


def recorded_names(conn: psycopg.Connection, model: str) -> frozenset[str]:
    """Every file name already recorded for a model, across all its runs.

    The scan loop calls this once per pass so it can skip old files
    without opening them. Filenames include the run time, so a name
    never repeats between runs. (This set grows by ~800 names a day for
    HRDPS; fine for months. Limit it to recent runs if it ever gets slow.)
    """
    rows = conn.execute(
        """
        SELECT f.file_name
        FROM run_files f
        JOIN runs r ON r.id = f.run_id
        WHERE r.model = %s
        """,
        (model,),
    ).fetchall()
    return frozenset(name for (name,) in rows)


def oldest_run_time(conn: psycopg.Connection, model: str) -> datetime | None:
    """The run time of the model's first ever recorded run, or None.

    status.py never creates rows for runs before this, so a fresh system
    doesn't suddenly report 30 days of "missing" runs from before it existed.
    """
    return conn.execute(
        "SELECT min(run_time) FROM runs WHERE model = %s", (model,)
    ).fetchone()[0]


def unfinished_runs(conn: psycopg.Connection, model: str) -> list[tuple[int, datetime, str]]:
    """(id, run_time, status) of every run of the model not yet complete."""
    return conn.execute(
        """
        SELECT id, run_time, status FROM runs
        WHERE model = %s AND status <> 'complete'
        ORDER BY run_time
        """,
        (model,),
    ).fetchall()


def set_status(conn: psycopg.Connection, run_id: int, status: str) -> None:
    """Change a run's status. The CHECK in 001_runs.sql rejects unknown values."""
    conn.execute("UPDATE runs SET status = %s WHERE id = %s", (status, run_id))


def _require_utc(t: datetime, what: str) -> None:
    """Refuse naive or non-UTC times, so only UTC ever reaches the database.

    (Postgres would accept a naive time and silently assume its own time
    zone. Ours is UTC, but relying on that would be fragile.)
    """
    if t.utcoffset() != timedelta(0):
        raise ValueError(f"{what} must be timezone-aware UTC, got {t!r}")
