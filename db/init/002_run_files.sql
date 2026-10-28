-- One row per file received for a run (build step 5).
-- AMQP delivers at least once, so the same notice can arrive twice;
-- UNIQUE (run_id, file_name) lets the ingest insert with
-- ON CONFLICT DO NOTHING and count each file exactly once.
--
-- published_at is when ECCC published the file, not when we downloaded
-- it: sr3's timeCopy option copies ECCC's timestamp onto the saved file,
-- and the ingest reads it from there. Using ECCC's time means our own
-- delays (laptop asleep, ingest down) don't make runs look late.
--
-- Renamed from seen_at on 2026-10-28. The live database was changed with
--   ALTER TABLE run_files RENAME COLUMN seen_at TO published_at;
-- because init scripts only run on an empty volume.

CREATE TABLE run_files (
    run_id        bigint      NOT NULL REFERENCES runs (id),
    file_name     text        NOT NULL,
    published_at  timestamptz NOT NULL,
    UNIQUE (run_id, file_name)
);
