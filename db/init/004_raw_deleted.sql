-- Marks a run whose raw GRIB files have been deleted (ingest/cleanup.py).
-- NULL = the files are still on disk. Set = the run can't be re-extracted
-- from disk; the Datamart keeps its own copy for 30 days.
--
-- Safe to run by hand on an existing database (see 003_point_forecasts.sql):
--   docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB' < db/init/004_raw_deleted.sql

ALTER TABLE runs ADD COLUMN IF NOT EXISTS raw_deleted_at timestamptz;
