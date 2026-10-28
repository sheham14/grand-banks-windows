"""Run status: is each run complete, late or incomplete? (build step 5, piece 6)

Four statuses (the CHECK in db/init/001_runs.sql allows only these):

    receiving   files are arriving and it's not past the "late" deadline
    complete    every expected file is recorded
    late        past run time + late_after, still missing files;
                the fallback starts requesting them by name
    incomplete  past run time + give_up_after, still missing files;
                the fallback keeps trying within the backfill window

Status is never stored by hand: it's worked out from facts (what's
missing, how old the run is) on every pass. So a late run that the
fallback fills becomes complete by itself, and a status can't drift out
of step with the files. The deadlines live in config/models.yaml.

The non-obvious part is find-missing-runs. A run normally gets its row
when its first file arrives. If ECCC skipped a run, or our feed was down,
no file ever arrives, so nothing would ever notice. You can't detect a
missing run by waiting for its files; you have to know from the clock
which runs should exist. ensure_expected_runs() does that.
"""

import logging
from datetime import datetime, timedelta

import psycopg

from ingest.db import (
    get_or_create_run,
    oldest_run_time,
    received_files,
    set_status,
    unfinished_runs,
)
from ingest.models import ModelConfig, expected_files

log = logging.getLogger("ingest.status")

RECEIVING = "receiving"
COMPLETE = "complete"
LATE = "late"
INCOMPLETE = "incomplete"


def decide_status(
    missing: int,
    run_time: datetime,
    now: datetime,
    late_after: timedelta,
    give_up_after: timedelta,
) -> str:
    """The status a run should have, from how many files are missing and its age.

    A pure function (no database, no clock: `now` is passed in), so every
    edge case can be tested directly.
    """
    # Complete wins over everything: a run that was late but got filled
    # is complete, not "late".
    if missing == 0:
        return COMPLETE
    age = now - run_time
    # >= : exactly at the deadline counts as past it.
    if age >= give_up_after:
        return INCOMPLETE
    if age >= late_after:
        return LATE
    return RECEIVING


def expected_run_times(model: ModelConfig, start: datetime, end: datetime) -> list[datetime]:
    """Every run time of the model from start to end, both included, oldest first.

    E.g. HRDPS from Oct 28 05Z to Oct 28 13Z -> [06Z, 12Z].
    """
    times = []
    # Walk day by day from midnight of the start day, adding each run hour.
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for hour in model.run_hours:
            t = day + timedelta(hours=hour)
            if start <= t <= end:
                times.append(t)
        day += timedelta(days=1)
    return times


def ensure_expected_runs(conn: psycopg.Connection, model: ModelConfig, now: datetime) -> None:
    """Create rows for runs that should have started arriving by now.

    "Should have started" = run time + late_after has passed. Earlier than
    that, rows appear normally when the first file comes in.

    Looks back no further than the backfill window, and never before the
    model's first recorded run (so a fresh system doesn't report runs
    from before it existed). With nothing recorded yet, it does nothing
    and waits for the first file.
    """
    oldest = oldest_run_time(conn, model.name)
    if oldest is None:
        return
    start = max(oldest, now - model.backfill)
    for run_time in expected_run_times(model, start, now - model.late_after):
        # get_or_create_run does nothing to a run that already has a row.
        get_or_create_run(conn, model.name, run_time, model.files_per_run)


def update_statuses(
    conn: psycopg.Connection, model: ModelConfig, now: datetime
) -> list[tuple[datetime, frozenset[str]]]:
    """Bring every unfinished run's status up to date.

    Returns (run_time, missing names) for each run the fallback should
    work on: late or incomplete, and inside the backfill window.
    """
    ensure_expected_runs(conn, model, now)

    for_fallback = []
    for run_id, run_time, old_status in unfinished_runs(conn, model.name):
        missing = expected_files(model, run_time) - received_files(conn, run_id)
        new_status = decide_status(
            len(missing), run_time, now, model.late_after, model.give_up_after
        )
        if new_status != old_status:
            set_status(conn, run_id, new_status)
            log.info("%s %s: %s -> %s (%d missing)", model.name,
                     f"{run_time:%Y-%m-%d %HZ}", old_status, new_status, len(missing))
        if new_status in (LATE, INCOMPLETE) and run_time >= now - model.backfill:
            for_fallback.append((run_time, missing))
    return for_fallback
