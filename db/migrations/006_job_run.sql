-- One row per data job run (sync, import, recompute), however it was started.
CREATE TABLE job_run (
    id          bigserial PRIMARY KEY,
    job         text NOT NULL,             -- sync | import | recompute
    trigger     text NOT NULL,             -- cli | button | schedule
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    summary     text,                      -- report lines; the first is a one-line headline
    error       text
);

CREATE INDEX job_run_job_started_idx ON job_run (job, started_at DESC);
