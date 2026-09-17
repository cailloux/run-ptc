-- Raw GPS is coordinate-system-agnostic: store it in WGS84 lon/lat instead of
-- projecting eagerly into whichever network happens to exist today, so a
-- track can be reclassified against a network added later without refetching
-- it from Intervals.
ALTER TABLE activity ALTER COLUMN track_raw TYPE geometry(LineString, 4326)
    USING ST_Transform(track_raw, 4326);

-- Each network's segment extent, transformed to 4326 once, so sync and
-- recompute can classify a raw (4326) track against every network with a
-- single ST_Intersects test, before knowing which network (and therefore
-- which SRID) it belongs to.
CREATE VIEW network_box AS
SELECT s.network_id, n.srid,
       ST_Transform(ST_SetSRID(ST_Extent(s.geom)::geometry, n.srid), 4326) AS box_4326
FROM segment s
JOIN network n ON n.id = s.network_id
GROUP BY s.network_id, n.srid;
