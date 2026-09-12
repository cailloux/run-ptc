-- Why a segment doesn't count toward totals: 'type' (a cart path type that
-- doesn't count), 'outside city', or 'second carriageway' (the other side of
-- a divided road, which counts once).
ALTER TABLE segment ADD COLUMN uncounted_reason text;
UPDATE segment SET uncounted_reason = CASE layer WHEN 'cartpath' THEN 'type' ELSE 'outside city' END
WHERE NOT counted;

-- Routing prefers cart paths over roads running alongside them by raising
-- those roads' cost; length_m keeps the true length for distances.
ALTER TABLE route_edge ADD COLUMN length_m double precision;
UPDATE route_edge SET length_m = cost;
ALTER TABLE route_edge ALTER COLUMN length_m SET NOT NULL;

-- The share of a line lying within dist of another geometry, from points
-- sampled every 5 m. Used instead of ST_Length(ST_Intersection(line,
-- ST_Buffer(other, dist))) because GEOS 3.9.0, in the pgrouting image,
-- returns EMPTY for that intersection when the line is exactly horizontal
-- or vertical. ST_DWithin doesn't go through the overlay code at all.
-- Not STRICT, so Postgres can inline it.
CREATE FUNCTION share_within(line geometry, other geometry, dist double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT avg(CASE WHEN ST_DWithin(ST_LineInterpolatePoint(d.geom, i::float8 / k.n), other, dist)
                    THEN 1.0 ELSE 0.0 END)
    FROM ST_Dump(line) d
    CROSS JOIN LATERAL (SELECT GREATEST(1, CEIL(ST_Length(d.geom) / 5))::int AS n) k
    CROSS JOIN LATERAL generate_series(0, k.n) i
$$;
