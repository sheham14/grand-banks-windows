-- One row per model run (build step 5).
-- All times are timezone-aware UTC (timestamptz).
-- Files received are counted from run_files, not stored here,
-- so a duplicate notice can't inflate the count.

CREATE TABLE runs (
    id              bigserial   PRIMARY KEY,
    model           text        NOT NULL,
    run_time        timestamptz NOT NULL,
    first_file_at   timestamptz,
    last_file_at    timestamptz,
    files_expected  integer     NOT NULL CHECK (files_expected > 0),
    status          text        NOT NULL DEFAULT 'receiving'
                    CHECK (status IN ('receiving', 'late', 'complete', 'incomplete')),
    UNIQUE (model, run_time)
);
