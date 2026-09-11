"""Coverage state per node interval, as GeoJSON for the map.

Each part of a counted, non-excluded segment is cut into runs of
consecutive node intervals that are all covered (both end nodes hit) or all
not covered, one feature per run. Cart path segments with every node hit
come back whole as `complete`. Excluded and uncounted segments come back
whole with that state. Intervals never span parts.
"""

import psycopg

COVERAGE_SQL = """
WITH seg AS (
    SELECT s.id, s.layer, s.source_oid, s.source_key, s.name, s.seg_type, s.counted,
           s.excluded, s.exclusion_reason, s.length_m, s.geom,
           count(n.id) AS nodes_total, count(n.hit_at) AS nodes_hit
    FROM segment s LEFT JOIN node n ON n.segment_id = s.id
    WHERE s.layer = %(layer)s
    GROUP BY s.id
), whole AS (
    -- NULL state means "cut into interval runs".
    SELECT seg.*, CASE
        WHEN NOT counted OR nodes_total = 0 THEN 'uncounted'
        WHEN excluded THEN 'excluded'
        WHEN layer = 'cartpath' AND nodes_hit = nodes_total THEN 'complete'
    END AS state
    FROM seg
), iv AS (
    -- Interval (seq - 1, seq) is covered when both of its nodes are hit.
    SELECT n.segment_id, n.part_idx, n.seq - 1 AS a, n.seq AS b,
           n.hit_at IS NOT NULL AND lag(n.hit_at IS NOT NULL) OVER w AS covered,
           max(n.seq) OVER (PARTITION BY n.segment_id, n.part_idx) AS intervals
    FROM node n JOIN whole ON whole.id = n.segment_id AND whole.state IS NULL
    WINDOW w AS (PARTITION BY n.segment_id, n.part_idx ORDER BY n.seq)
), runs AS (
    -- Gaps and islands: consecutive intervals with the same state share g.
    SELECT segment_id, part_idx, covered, min(a) AS a, max(b) AS b, max(intervals) AS intervals
    FROM (
        SELECT *, a - row_number() OVER (PARTITION BY segment_id, part_idx, covered ORDER BY a) AS g
        FROM iv WHERE a >= 0
    ) x
    GROUP BY segment_id, part_idx, covered, g
), pieces AS (
    SELECT r.segment_id, CASE WHEN r.covered THEN 'run' ELSE 'not_run' END AS state,
           ST_LineSubstring(ST_GeometryN(whole.geom, r.part_idx + 1),
                            r.a::float8 / r.intervals, r.b::float8 / r.intervals) AS geom
    FROM runs r JOIN whole ON whole.id = r.segment_id
    UNION ALL
    SELECT id, state, geom FROM whole WHERE state IS NOT NULL
)
SELECT json_build_object('type', 'FeatureCollection', 'features',
    coalesce(json_agg(json_build_object(
        'type', 'Feature',
        'geometry', ST_AsGeoJSON(ST_Transform(p.geom, 4326), 6)::json,
        'properties', json_build_object(
            'segment_id', w.id,
            'source_oid', w.source_oid,
            'source_key', w.source_key,
            'state', p.state,
            'length_m', round(ST_Length(p.geom)::numeric, 1),
            'segment_length_m', round(w.length_m::numeric, 1),
            'name', w.name,
            'seg_type', w.seg_type,
            'parts', ST_NumGeometries(w.geom),
            'nodes_hit', w.nodes_hit,
            'nodes_total', w.nodes_total,
            'exclusion_reason', w.exclusion_reason)
    ) ORDER BY w.id, p.state), '[]'::json))::text
FROM pieces p JOIN whole w ON w.id = p.segment_id
"""


def coverage_geojson(conn: psycopg.Connection, layer: str) -> str:
    return conn.execute(COVERAGE_SQL, {"layer": layer}).fetchone()[0]
