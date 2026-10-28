-- One row per file seen for a run (build step 5).
-- AMQP delivers at least once, so the same notice can arrive twice;
-- UNIQUE (run_id, file_name) lets the ingest insert with
-- ON CONFLICT DO NOTHING and count each file exactly once.

CREATE TABLE run_files (
    run_id     bigint      NOT NULL REFERENCES runs (id),
    file_name  text        NOT NULL,
    seen_at    timestamptz NOT NULL,
    UNIQUE (run_id, file_name)
);
