"""The ingest job's main loop (build step 5, piece 5).

sr3 downloads files into data/raw/<model>/. This job looks at those
folders every minute and records each new, sound file in the database.
It ties the earlier pieces together:

    filenames.py  parse the name          -> model, run time, lead hours
    models.py     what a full run has     -> 196 / 98 files
    gribcheck.py  is the file sound?      -> 7777, 1 message, header matches
    db.py         record it               -> runs, run_files
    status.py     complete / late / ...   -> runs.status, missing runs found
    fallback.py   fetch what's missing    -> by known name, never a listing

Why a separate loop instead of a hook inside sr3: files from sr3 and,
later, from the fallback (piece 6) land in the same folders, so one code
path handles both. And if this job stops, nothing is lost: the next scan
picks up every file not yet in run_files.

Listing our OWN folders is fine. Only Datamart directory listings are
forbidden.

One pass = scan the folders, update every unfinished run's status, then
let the fallback fetch files for late runs. Files the fallback saves are
recorded by the next pass, through the same checks as sr3's files.

Run it:
    python -m ingest.scan          # every 60 seconds until Ctrl+C
    python -m ingest.scan --once   # one pass, then exit
"""

import argparse
import logging
import time
from datetime import datetime, timezone

import psycopg

from ingest.db import connect, get_or_create_run, received_files, recorded_names, record_file
from ingest.fallback import Fallback
from ingest.filenames import parse_filename
from ingest.gribcheck import GribCheckError, check_grib
from ingest.models import ModelConfig, expected_files, load_models
from ingest.status import update_statuses

log = logging.getLogger("ingest.scan")

# Names we've already warned about, so a file that keeps failing is
# logged once, not every minute. Piece 6 will deal with such files
# (re-fetch, or mark the run incomplete).
_warned: set[str] = set()


def run_pass(
    conn: psycopg.Connection,
    models: dict[str, ModelConfig],
    fallback: Fallback,
    now: datetime | None = None,
) -> int:
    """One full pass: scan, update statuses, fetch for late runs.

    Returns how many new files were recorded. `now` can be passed in by
    tests; normally it's the current time in UTC.
    """
    now = now or datetime.now(timezone.utc)
    new = scan_once(conn, models)
    for model in models.values():
        for run_time, missing in update_statuses(conn, model, now):
            fallback.attempt(model, run_time, missing, now)
    return new


def scan_once(conn: psycopg.Connection, models: dict[str, ModelConfig]) -> int:
    """One pass over every model's folder. Returns how many files were new."""
    return sum(_scan_model(conn, model) for model in models.values())


def _scan_model(conn: psycopg.Connection, model: ModelConfig) -> int:
    """Record every new, sound file in one model's folder. Returns the count."""
    # One query for every name already recorded for this model, instead
    # of opening and checking ~600 files each minute just to find out
    # they're old.
    done = recorded_names(conn, model.name)

    new = 0
    new_per_run: dict[int, datetime] = {}  # runs.id -> run time, runs that grew
    expected: dict[datetime, frozenset[str]] = {}  # run time -> expected names

    # sorted(): a stable order makes logs easier to read. Nothing relies
    # on it; record_file() copes with any order (the LEAST/GREATEST logic).
    for path in sorted(model.folder.glob("*.grib2")):
        if path.name in done:
            continue

        try:
            f = parse_filename(path)
        except ValueError as e:
            _warn_once(path.name, str(e))
            continue

        # A file in the wrong folder, or from a run hour the model
        # doesn't have, points at a config mistake: report, don't record.
        if f.model != model.name:
            _warn_once(path.name, f"{path.name}: {f.model} file in the {model.name} folder")
            continue
        if f.run_time.hour not in model.run_hours:
            _warn_once(path.name, f"{path.name}: {model.name} has no {f.run_time:%H}Z run")
            continue
        # Only files models.yaml expects count. A stray variable or a lead
        # hour past 48 would otherwise be recorded without ever counting
        # towards a complete run.
        if f.run_time not in expected:
            expected[f.run_time] = expected_files(model, f.run_time)
        if f.name not in expected[f.run_time]:
            _warn_once(path.name, f"{path.name}: not a file models.yaml expects")
            continue

        try:
            check_grib(path, f)
        except GribCheckError as e:
            # Usually a file sr3 is still writing: it'll pass next minute.
            _warn_once(path.name, str(e))
            continue

        run_id = get_or_create_run(conn, model.name, f.run_time, model.files_per_run)

        # The file's modification time is ECCC's publish time, because
        # sr3's timeCopy option copies it onto the downloaded file.
        published_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        # False would mean another process recorded it between our
        # recorded_names() query and now; harmless, just not new.
        if record_file(conn, run_id, f.name, published_at):
            new += 1
            new_per_run[run_id] = f.run_time
            _warned.discard(path.name)  # it failed before, but it's fine now

    # One progress line per run that got new files in this pass.
    for run_id, run_time in sorted(new_per_run.items(), key=lambda item: item[1]):
        have = len(received_files(conn, run_id))
        log.info("%s %s: %d/%d files", model.name, f"{run_time:%Y-%m-%d %HZ}",
                 have, model.files_per_run)
    return new


def _warn_once(name: str, message: str) -> None:
    """Log a warning the first time a given file has a problem."""
    if name not in _warned:
        _warned.add(name)
        log.warning(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record downloaded model files in Postgres.")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between passes")
    args = parser.parse_args()

    # Log times in UTC with a Z, like everything else in the project.
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    models = load_models()
    fallback = Fallback()
    conn = None
    while True:
        try:
            # (Re)connect if needed: the first pass, or after Postgres
            # went away. The loop keeps going instead of crashing, which
            # matters for a job that has to run unattended for days.
            if conn is None or conn.closed:
                conn = connect()
            new = run_pass(conn, models, fallback)
            if new:
                log.info("pass done: %d new file(s)", new)
        except psycopg.OperationalError as e:
            log.error("database unavailable, will retry: %s", e)
            conn = None

        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopped")
            break


# Runs main() only when started as a program (python -m ingest.scan),
# not when another module or a test imports this file.
if __name__ == "__main__":
    main()
