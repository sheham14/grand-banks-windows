"""Delete raw GRIB files we no longer need (Phase 1, keeps the VM's disk from filling).

Raw files arrive at about 2.2 GB a day, and the values we want are in
point_forecasts once a run is extracted. A run's raw files are deleted
only if ALL of these are true:

  1. It has been extracted (runs.extracted_at is set).
  2. The extractor didn't refuse it (runs.extract_error is empty). A
     refused run keeps its files, for debugging and for the retry.
  3. It's older than keep_raw_days in config/models.yaml (5 days). That
     has to be longer than the fallback's 48 hours: until then, a run
     can still become complete and be extracted again. models.py refuses
     a config where it isn't.

Files are deleted by exact name, from models.expected_files(), never by
a pattern or by listing a folder. So the buoy archive (data/raw/swob),
data/explore and anything unexpected can't be touched.

Deleted files don't come back. The fallback works out what's missing
from the database (expected minus run_files), and run_files keeps its
rows, so a deleted file still counts as received. sr3 only acts on new
notices. The arrival-time history stays in the database too.

Deleting is permanent on our disk; the Datamart keeps its own copy for
30 days, fetchable by name. Once a run's files are gone, runs.raw_deleted_at
is set: it stops the run being rechecked every hour, and shows at a glance
that the run can't be re-extracted from disk.

Refused runs keep their files, so a bug that refused every run would
fill the disk in about 4 to 5 days. That's why each pass also warns
when free space is low.

The ingest job (scan.py) calls run_cleanup() once an hour. By hand:
    python -m ingest.cleanup --dry-run                # what would go; deletes nothing
    python -m ingest.cleanup --dry-run --keep-days 3  # preview a shorter window
    python -m ingest.cleanup                          # delete now
"""

import argparse
import logging
import shutil
from datetime import datetime, timedelta, timezone

import psycopg

from ingest.db import REPO_ROOT, connect, mark_raw_deleted, runs_to_clean
from ingest.models import ModelConfig, expected_files, load_models

log = logging.getLogger("ingest.cleanup")

# Warn below this much free space. About a day and a half of raw files.
LOW_DISK_GB = 3.0


def clean_model(
    conn: psycopg.Connection,
    model: ModelConfig,
    now: datetime,
    dry_run: bool = False,
    keep: timedelta | None = None,
) -> tuple[int, int]:
    """Delete the raw files of every run of `model` that's due.

    keep overrides the config's keep_raw (for previews). A dry run only
    reports. Returns (files, bytes) deleted, or that would be deleted.
    """
    cutoff = now - (keep if keep is not None else model.keep_raw)
    total_files = total_bytes = 0

    for run_id, run_time in runs_to_clean(conn, model.name, cutoff):
        label = f"{model.name} {run_time:%Y-%m-%d %HZ}"
        files = size = 0
        try:
            for name in expected_files(model, run_time):
                path = model.folder / name
                if not path.exists():
                    continue  # an incomplete run never had it
                file_size = path.stat().st_size
                if not dry_run:
                    path.unlink()
                files += 1
                size += file_size
        except OSError as e:
            # Not marked, so the next pass tries this run again. Never
            # let a failed delete stop the ingest this runs inside.
            log.error("%s: delete failed, will retry: %s", label, e)
            total_files += files
            total_bytes += size
            continue

        if dry_run:
            log.info("%s: would delete %d files, %.0f MB", label, files, size / 1e6)
        else:
            mark_raw_deleted(conn, run_id)
            log.info("%s: deleted %d raw files, %.0f MB", label, files, size / 1e6)
        total_files += files
        total_bytes += size
    return total_files, total_bytes


def warn_if_low_disk() -> bool:
    """Log a warning if free space on the data disk is low. Returns True if so."""
    free_gb = shutil.disk_usage(REPO_ROOT / "data").free / 1e9
    if free_gb < LOW_DISK_GB:
        log.warning("low disk: %.1f GB free. Refused runs keep their files; check "
                    "runs.extract_error.", free_gb)
        return True
    return False


def run_cleanup(
    conn: psycopg.Connection, models: dict[str, ModelConfig], now: datetime | None = None
) -> None:
    """One cleanup pass over every model, then the disk check."""
    now = now or datetime.now(timezone.utc)
    for model in models.values():
        clean_model(conn, model, now)
    warn_if_low_disk()


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete raw GRIB files of old, extracted runs.")
    parser.add_argument("--dry-run", action="store_true", help="report only; delete nothing")
    parser.add_argument("--keep-days", type=float, help="preview another window (needs --dry-run)")
    args = parser.parse_args()
    # A preview can use any number, but real deletes always follow the config.
    if args.keep_days is not None and not args.dry_run:
        parser.error("--keep-days only works with --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    keep = timedelta(days=args.keep_days) if args.keep_days is not None else None
    now = datetime.now(timezone.utc)
    files = size = 0
    with connect() as conn:
        for model in load_models().values():
            model_files, model_size = clean_model(conn, model, now, args.dry_run, keep)
            files += model_files
            size += model_size
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {files} files, {size / 1e9:.2f} GB")
    print(f"free: {shutil.disk_usage(REPO_ROOT / 'data').free / 1e9:.1f} GB")
    warn_if_low_disk()


# Runs main() only when started as a program, not when imported.
if __name__ == "__main__":
    main()
