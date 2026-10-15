-- 0004_conversions.sql
-- Conversion uploads: when the status changed (what the platform is told) and when we last tried.
-- Plus the nightly job bookkeeping for spend ingestion.

ALTER TABLE conversion_event
    ADD COLUMN event_time   timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN attempted_at timestamptz;

-- One row per named job per day, so a job driven by the minute tick runs once.
CREATE TABLE job_run (
    name        text NOT NULL,
    ran_on      date NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (name, ran_on)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON job_run TO crm_app;
