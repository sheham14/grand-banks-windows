-- Forecast values at our sites, one row per number (build step 7).
--
-- One row per (run, member, site, variable, lead hour), the "long" shape:
--   HRDPS 2026-10-28 12Z | nickerson_bank | wind_speed     | 24 | 11.06  | m/s
--   HRDPS 2026-10-28 12Z | nickerson_bank | wind_direction | 24 | 268.41 | degrees
-- New models, ensemble members and variables are just more rows; the
-- table never changes shape. About 1,600 rows a day for two models.
--
-- Model and run time aren't repeated here: run_id points at the runs
-- row that already holds them, so the two can never disagree.
--
-- Written so it can also be run BY HAND on an existing database (init
-- scripts only run on an empty one): every statement says IF NOT EXISTS,
-- so running it twice does nothing the second time.
--   docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB' < db/init/003_point_forecasts.sql

ALTER TABLE runs
    -- When the extractor stored this run's values. NULL = not yet.
    ADD COLUMN IF NOT EXISTS extracted_at  timestamptz,
    -- Why the extractor refused the run (e.g. directions disagree with
    -- ECCC's WDIR). NULL = no problem. A refused run isn't retried until
    -- a person clears this, after fixing the cause.
    ADD COLUMN IF NOT EXISTS extract_error text;

CREATE TABLE IF NOT EXISTS point_forecasts (
    run_id      bigint           NOT NULL REFERENCES runs (id),
    member      integer,                    -- ensemble member; NULL for deterministic models
    site_id     text             NOT NULL,  -- from config/sites.yaml
    variable    text             NOT NULL,  -- our name, e.g. wind_speed
    lead_hours  integer          NOT NULL CHECK (lead_hours >= 0),
    valid_time  timestamptz      NOT NULL,  -- run time + lead_hours, UTC
    value       double precision NOT NULL,
    unit        text             NOT NULL,  -- every stored value carries its unit
    -- Each number is stored once, so extracting a run again never
    -- doubles it. NULLS NOT DISTINCT matters: by default Postgres treats
    -- two NULLs as DIFFERENT, so with member NULL (every HRDPS row) a
    -- plain UNIQUE would let every duplicate in. (Postgres 15 and later.)
    UNIQUE NULLS NOT DISTINCT (run_id, member, site_id, variable, lead_hours)
);
