-- A stretch of time a data source (sync, city) was unhealthy. The open row is
-- the marker that keeps its alert from repeating; closed rows are history for
-- the status page.
CREATE TABLE alert_episode (
    id           bigserial PRIMARY KEY,
    source       text NOT NULL CHECK (source IN ('sync', 'city')),
    problem      text NOT NULL CHECK (problem IN ('failing', 'stale')),
    detail       text,               -- what was wrong when it opened
    since        timestamptz NOT NULL,
    alerted_at   timestamptz,        -- the alert was sent
    recovered_at timestamptz,        -- the source was seen healthy again
    closed_at    timestamptz         -- done: recovery notice sent, or none was owed
);

CREATE UNIQUE INDEX alert_episode_open_idx ON alert_episode (source) WHERE closed_at IS NULL;

-- When the city last added or changed a segment, for the map's "latest city
-- changes" layer. Null for segments unchanged since the layer's first import.
ALTER TABLE segment
    ADD COLUMN city_change text CHECK (city_change IN ('added', 'changed')),
    ADD COLUMN changed_at  timestamptz;
