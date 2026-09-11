-- City tracks cut into short pieces for matching. A whole track's bounding
-- box usually covers most of the city, so it can't narrow a spatial join;
-- ~200 m pieces can. Rebuilt from activity.geom by sync and recompute.
CREATE TABLE activity_piece (
    activity_id bigint NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    start_at    timestamptz NOT NULL,
    geom        geometry(LineString, 32616) NOT NULL
);

CREATE INDEX activity_piece_geom_idx ON activity_piece USING gist (geom);
CREATE INDEX activity_piece_activity_idx ON activity_piece (activity_id);
CREATE INDEX node_hit_activity_idx ON node (hit_activity_id);

-- Cut each part of a track into equal pieces of at most piece_m.
-- Deliberately not STRICT: Postgres only inlines set-returning SQL functions
-- that aren't strict, and without inlining a full rebuild is ~25x slower.
CREATE OR REPLACE FUNCTION track_pieces(track geometry, piece_m double precision)
RETURNS SETOF geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT ST_LineSubstring(d.geom, i::float8 / k.n, (i + 1)::float8 / k.n)
    FROM ST_Dump(track) d
    CROSS JOIN LATERAL (SELECT GREATEST(1, CEIL(ST_Length(d.geom) / piece_m))::int AS n) k
    CROSS JOIN LATERAL generate_series(0, k.n - 1) i
$$;
