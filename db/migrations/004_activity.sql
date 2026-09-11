-- Runs synced from Intervals.icu. Every GPS run gets a row so later syncs
-- don't refetch its stream, but tracks are kept only for runs that
-- intersect the city's bounding box.
CREATE TABLE activity (
    id           bigserial PRIMARY KEY,
    intervals_id text NOT NULL UNIQUE,
    start_at     timestamptz NOT NULL,
    sport        text NOT NULL,
    name         text,
    distance_m   double precision,
    source       text,
    status       text NOT NULL CHECK (status IN ('city', 'outside', 'no_gps')),
    track_raw    geometry(LineString, 32616),       -- every GPS fix in order, so splitting can be redone
    geom         geometry(MultiLineString, 32616),  -- track_raw split at GPS gaps; used for matching
    synced_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX activity_track_raw_idx ON activity USING gist (track_raw);
CREATE INDEX activity_geom_idx ON activity USING gist (geom);
CREATE INDEX activity_start_at_idx ON activity (start_at);

ALTER TABLE node
    ADD CONSTRAINT node_hit_activity_fk
    FOREIGN KEY (hit_activity_id) REFERENCES activity (id) ON DELETE SET NULL;

-- Split a track wherever consecutive fixes are more than gap_m apart, so a
-- GPS jump or a pause-and-resume doesn't draw a straight line across town.
-- Parts with fewer than two fixes or no length are dropped. Returns NULL if
-- nothing is left.
CREATE FUNCTION split_track(track geometry, gap_m double precision)
RETURNS geometry
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
    WITH pts AS (
        SELECT d.path[1] AS i, d.geom FROM ST_DumpPoints(track) d
    ), breaks AS (
        SELECT i, geom,
               CASE WHEN ST_Distance(geom, lag(geom) OVER (ORDER BY i)) > gap_m THEN 1 ELSE 0 END AS brk
        FROM pts
    ), grouped AS (
        SELECT i, geom, sum(brk) OVER (ORDER BY i) AS grp FROM breaks
    ), parts AS (
        SELECT grp, ST_MakeLine(geom ORDER BY i) AS g
        FROM grouped GROUP BY grp HAVING count(*) >= 2
    )
    SELECT ST_Multi(ST_Collect(g ORDER BY grp)) FROM parts WHERE ST_Length(g) > 0
$$;
