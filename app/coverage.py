"""Coverage state per node interval, as GeoJSON for the map.

Each part of a counted, non-excluded segment is cut into runs of
consecutive node intervals that are all covered (both end nodes hit) or all
not covered, one feature per run. Cart path segments with every node hit
come back whole as `complete`. Excluded and uncounted segments come back
whole with that state. Intervals never span parts.

A divided road's second carriageway has no nodes and is dropped here
entirely, so the map shows one line per divided road. It's still a
routable edge (app/graph.py reads segment geometry directly); only display
and totals treat it as absent.
"""

import psycopg

COVERAGE_SQL = """
WITH seg AS (
    SELECT s.id, s.layer, s.source_oid, s.source_key, s.name, s.seg_type, s.counted,
           s.uncounted_reason, s.excluded, s.exclusion_reason, s.length_m, s.geom,
           s.city_change, s.changed_at,
           count(n.id) AS nodes_total, count(n.hit_at) AS nodes_hit
    FROM segment s LEFT JOIN node n ON n.segment_id = s.id
    WHERE s.layer = %(layer)s AND s.uncounted_reason IS DISTINCT FROM 'second carriageway'
    GROUP BY s.id
), whole AS (
    -- NULL state means "cut into interval runs".
    SELECT seg.*, CASE
        WHEN NOT counted OR nodes_total = 0 THEN 'uncounted'
        WHEN excluded THEN 'excluded'
        WHEN layer = 'cartpath' AND nodes_hit = nodes_total THEN 'complete'
    END AS state
    FROM seg
), pt AS (
    SELECT n.segment_id, n.part_idx, n.seq, n.hit_at IS NOT NULL AS hit
    FROM node n JOIN whole w ON w.id = n.segment_id
    WHERE w.state IS NULL
), iv AS (
    -- Interval (seq - 1, seq) is run when both ends are hit.
    SELECT segment_id, part_idx, seq - 1 AS a, seq AS b,
           CASE WHEN hit IS NULL OR lag(hit) OVER w IS NULL THEN 'uncounted'
                WHEN hit AND lag(hit) OVER w THEN 'run'
                ELSE 'not_run' END AS state,
           max(seq) OVER (PARTITION BY segment_id, part_idx) AS intervals
    FROM pt
    WINDOW w AS (PARTITION BY segment_id, part_idx ORDER BY seq)
), runs AS (
    -- Gaps and islands: consecutive intervals with the same state share g.
    SELECT segment_id, part_idx, state, min(a) AS a, max(b) AS b, max(intervals) AS intervals
    FROM (
        SELECT *, a - row_number() OVER (PARTITION BY segment_id, part_idx, state ORDER BY a) AS g
        FROM iv WHERE a >= 0
    ) x
    GROUP BY segment_id, part_idx, state, g
), pieces AS (
    SELECT r.segment_id, r.state,
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
            'uncounted_reason', w.uncounted_reason,
            'exclusion_reason', w.exclusion_reason,
            'city_change', w.city_change,
            'changed_at', w.changed_at)
    ) ORDER BY w.id, p.state), '[]'::json))::text
FROM pieces p JOIN whole w ON w.id = p.segment_id
"""


def coverage_geojson(conn: psycopg.Connection, layer: str) -> str:
    return conn.execute(COVERAGE_SQL, {"layer": layer}).fetchone()[0]
