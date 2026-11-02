"""Extract each finished run once and store its values (build step 7).

The ingest job (scan.py) records files and decides when a run is
finished. This job takes it from there: it picks a finished run that
hasn't been extracted, runs the extractor (extract.py) over all of it,
and saves the values in point_forecasts, marking the run extracted.

    runs.status complete/incomplete, extracted_at empty
        -> locate the sites on this run's own grid   (sitegrid.py)
        -> extract every hour, checked against WDIR  (extract.py)
        -> save rows + extracted_at together         (db.save_point_values)

Why a separate job (its own Compose service) and not part of the scan:
extracting a run decodes ~150 files at ~2 seconds each, so it takes
minutes. Inside the scan loop that would hold up recording new files
and the fallback. Separate, a crash here can't stop the ingest either.

Choices worth knowing:
  - Newest run first: after an outage the latest forecast is ready
    first, and the backlog follows.
  - The sites are located on each run's own first file (about 3 s), so
    a grid change from ECCC can never pair old cells with new files.
  - A refused run (the extractor raised, e.g. directions disagree with
    WDIR) gets its reason saved in runs.extract_error and is NOT retried.
    Retrying a minutes-long job every minute would tie up the VM's CPU,
    and a refusal means a bug for a person to look at. After fixing it:
        UPDATE runs SET extract_error = NULL WHERE id = <run id>;
  - An "incomplete" run is extracted with its gaps. If its missing files
    turn up later and it becomes complete, db.set_status clears
    extracted_at and the run is extracted again to fill the gaps.

Run it:
    python -m ingest.store          # until Ctrl+C
    python -m ingest.store --once   # extract everything waiting, then exit
"""

import argparse
import logging
import time

import psycopg

from ingest.db import connect, mark_extract_failed, next_run_to_extract, save_point_values
from ingest.extract import extract_run, locate_for_run
from ingest.models import ModelConfig, load_models

log = logging.getLogger("ingest.store")


def extract_next(conn: psycopg.Connection, models: dict[str, ModelConfig]) -> bool:
    """Extract and store the newest waiting run. Returns False if none was waiting."""
    run = next_run_to_extract(conn, list(models))
    if run is None:
        return False
    run_id, model_name, run_time = run
    model = models[model_name]
    label = f"{model_name} {run_time:%Y-%m-%d %HZ}"

    log.info("%s: extracting", label)
    started = time.monotonic()
    try:
        cells = locate_for_run(model, run_time)
        values = extract_run(model, run_time, cells)
        if not values:
            # A finished run with no files on disk: deleted, or a wrong
            # folder. Storing "extracted, 0 values" would hide that.
            raise RuntimeError(f"no values extracted; are the files in {model.folder}?")
    except Exception as e:
        # ExtractError (the extractor refused the run), or anything else
        # that went wrong while reading files. Either way a person should
        # look: record why, log the full error, and move on.
        log.exception("%s: not stored", label)
        mark_extract_failed(conn, run_id, f"{type(e).__name__}: {e}")
        return True

    stored = save_point_values(conn, run_id, values)
    log.info("%s: stored, %d values, %.0f s", label, stored, time.monotonic() - started)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract finished runs and store their values.")
    parser.add_argument("--once", action="store_true", help="extract everything waiting, then exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between checks when idle")
    args = parser.parse_args()

    # Same log format as the scan: UTC times with a Z.
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    models = load_models()
    conn = None
    while True:
        did_one = False
        try:
            # (Re)connect if needed, like the scan: keep going through a
            # Postgres restart instead of crashing.
            if conn is None or conn.closed:
                conn = connect()
            did_one = extract_next(conn, models)
        except psycopg.OperationalError as e:
            log.error("database unavailable, will retry: %s", e)
            conn = None

        if did_one:
            continue  # more may be waiting: no pause between runs
        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopped")
            break


# Runs main() only when started as a program, not when imported.
if __name__ == "__main__":
    main()
