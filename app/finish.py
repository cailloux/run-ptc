"""New miles on a route, and routes that finish chosen segments.

Both work on node intervals, as the map does (app/coverage.py): interval k
of a part runs from node k-1 to node k and is run once both are hit. Nodes
sit at seq/n along their part, so interval k spans [(k-1)/n, k/n] of it,
which route_edge.part_from/part_to place on the graph.
"""

import bisect
import math
from dataclasses import dataclass

import psycopg

from app.routing import EDGES_SQL, RouteError, leg, snap

SAMPLE_M = 5     # new miles: the route is sampled this often
ON_EDGE_M = 1    # a sample this close to an edge is on it

# Not-run intervals of counted, non-excluded segments among {segments}, an
# SQL expression that yields segment ids.
NOT_RUN_SQL = """
    SELECT n.segment_id, n.part_idx, n.seq AS k,
           (n.seq - 1)::float8 / p.n AS a, n.seq::float8 / p.n AS b
    FROM node n
    JOIN node m ON m.segment_id = n.segment_id AND m.part_idx = n.part_idx AND m.seq = n.seq - 1
    JOIN (SELECT segment_id, part_idx, max(seq) AS n FROM node
          WHERE segment_id IN {segments} GROUP BY 1, 2) p
      ON p.segment_id = n.segment_id AND p.part_idx = n.part_idx
    JOIN segment s ON s.id = n.segment_id
    WHERE s.counted AND NOT s.excluded AND (n.hit_at IS NULL OR m.hit_at IS NULL)
"""

LatLng = tuple[float, float]


# ---- new miles -------------------------------------------------------------------


@dataclass
class NewMiles:
    new_m: float
    lines: list[list[LatLng]]   # the new stretches, for drawing


def new_miles(conn: psycopg.Connection, latlngs: list[LatLng]) -> NewMiles:
    """How much of a route runs over not-run intervals, each stretch counted once.

    The route is sampled every SAMPLE_M. A sample on an edge is new when the
    interval under it isn't run and no earlier sample passed the same spot of
    that part, so out-and-backs and retraces count once.
    """
    if len(latlngs) < 2:
        return NewMiles(0.0, [])
    rows = conn.execute(f"""
        WITH line AS (
            SELECT ST_Transform(ST_SetSRID(ST_MakeLine(ST_MakePoint(lon, lat) ORDER BY i), 4326), 32616) AS geom
            FROM unnest(%(lats)s::float8[], %(lons)s::float8[]) WITH ORDINALITY AS p(lat, lon, i)
        ), k AS (
            SELECT GREATEST(1, CEIL(ST_Length(geom) / %(sample_m)s))::int AS n, ST_Length(geom) AS len FROM line
        ), sample AS MATERIALIZED (
            -- One pass along the line: ST_LineInterpolatePoint per sample is
            -- quadratic in the route's vertex count.
            SELECT 0 AS i, ST_StartPoint(geom) AS pt FROM line
            UNION ALL
            SELECT coalesce(d.path[1], 1), d.geom
            FROM line, k, ST_Dump(ST_LineInterpolatePoints(line.geom, 1.0 / k.n, true)) d
        ), on_edge AS MATERIALIZED (
            -- f: the sample's position along its segment part, as a fraction.
            SELECT s.i, e.segment_id, e.part_idx, e.f,
                   e.f * ST_Length(ST_GeometryN(sg.geom, e.part_idx + 1)) AS along_m
            FROM sample s CROSS JOIN LATERAL (
                SELECT segment_id, part_idx, part_from + (part_to - part_from) * ST_LineLocatePoint(geom, s.pt) AS f
                FROM route_edge
                WHERE ST_DWithin(geom, s.pt, %(on_edge_m)s) ORDER BY geom <-> s.pt LIMIT 1
            ) e
            JOIN segment sg ON sg.id = e.segment_id
        ), iv AS MATERIALIZED ({NOT_RUN_SQL.format(segments="(SELECT segment_id FROM on_edge)")})
        -- A sample on a node belongs to both intervals; DISTINCT ON keeps one row.
        SELECT DISTINCT ON (s.i) (SELECT len / n FROM k), ST_Y(ST_Transform(s.pt, 4326)),
               ST_X(ST_Transform(s.pt, 4326)), o.segment_id, o.part_idx, o.along_m,
               iv.k IS NOT NULL AS not_run
        FROM sample s
        LEFT JOIN on_edge o ON o.i = s.i
        LEFT JOIN iv ON iv.segment_id = o.segment_id AND iv.part_idx = o.part_idx
                    AND o.f BETWEEN iv.a AND iv.b
        ORDER BY s.i, iv.k NULLS LAST
    """, {
        "lats": [p[0] for p in latlngs], "lons": [p[1] for p in latlngs],
        "sample_m": SAMPLE_M, "on_edge_m": ON_EDGE_M,
    }).fetchall()

    step = rows[0][0]
    passed: dict[tuple[int, int], list[float]] = {}   # per part: sorted along_m of samples so far
    new = []
    for _, _, _, segment_id, part_idx, along_m, not_run in rows:
        if segment_id is None:
            new.append(False)
            continue
        seen = passed.setdefault((segment_id, part_idx), [])
        j = bisect.bisect_left(seen, along_m)
        # The previous sample is a step away; a spot passed before is within half a step.
        near = any(abs(seen[x] - along_m) < 0.6 * step for x in (j - 1, j) if 0 <= x < len(seen))
        new.append(not_run and not near)
        bisect.insort(seen, along_m)

    # Each step counts by how many of its ends are new, so a stretch's ends
    # are off by at most half a step.
    new_m = step * sum(a + b for a, b in zip(new, new[1:])) / 2
    lines, run = [], []
    for is_new, row in zip(new + [False], rows + [None]):
        if is_new:
            run.append((row[1], row[2]))
        else:
            if len(run) >= 2:
                lines.append(run)
            run = []
    return NewMiles(new_m, lines)


# ---- finish segments ------------------------------------------------------------


@dataclass
class Unit:
    """A chain of edges covering not-run stretches of one segment part."""
    edges: list[tuple[int, int, int]]   # (id, source, target), in part order
    source: int        # vertex at the start of the first edge
    target: int        # vertex at the end of the last edge
    component: int


@dataclass
class Finish:
    start: LatLng
    legs: list[dict]   # latlngs, length_m, to: one per unit, then the way back
    length_m: float
    skipped: int       # units on islands the start can't reach


def units(conn: psycopg.Connection, segment_ids: list[int]) -> list[Unit]:
    """Edges of these segments that overlap a not-run interval, chained along each part.

    ponytail: a required edge is run end to end, even when only its far
    stretch is unrun; an out-and-back into part of an edge could be shorter.
    """
    rows = conn.execute(f"""
        WITH iv AS ({NOT_RUN_SQL.format(segments="(SELECT unnest(%(segments)s::bigint[]))")})
        SELECT DISTINCT e.id, e.segment_id, e.part_idx, e.part_from, e.source, e.target,
               v.component
        FROM route_edge e
        JOIN route_vertex v ON v.id = e.source
        JOIN iv ON iv.segment_id = e.segment_id AND iv.part_idx = e.part_idx
               AND least(e.part_to, iv.b) - greatest(e.part_from, iv.a) > 1e-9
        ORDER BY e.segment_id, e.part_idx, e.part_from
    """, {"segments": segment_ids}).fetchall()
    out: list[Unit] = []
    prev_part = None
    for edge_id, segment_id, part_idx, _, source, target, component in rows:
        if out and prev_part == (segment_id, part_idx) and out[-1].target == source:
            u = out[-1]
            u.edges.append((edge_id, source, target))
            u.target = target
        else:
            out.append(Unit([(edge_id, source, target)], source, target, component))
        prev_part = (segment_id, part_idx)
    return out


def _vertex_latlng(conn: psycopg.Connection, vid: int) -> LatLng:
    return conn.execute(
        "SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(geom, 4326) p FROM route_vertex WHERE id = %s) v",
        (vid,),
    ).fetchone()


def finish_route(conn: psycopg.Connection, start: LatLng, segment_ids: list[int],
                 max_m: float) -> Finish:
    """A loop from start that runs every not-run stretch of these segments."""
    a = snap(conn, *start, max_m)
    found = units(conn, segment_ids)
    todo = [u for u in found if u.component == a.component]
    if not todo:
        raise RouteError("those segments can't be reached from the start (they're on an island)"
                         if found else "nothing left to run on those segments")

    # For ordering, the start counts as the nearer end of its edge.
    source, target = conn.execute(
        "SELECT source, target FROM route_edge WHERE id = %s", (a.edge_id,)).fetchone()
    home = source if a.fraction < 0.5 else target
    vids = sorted({home, *(u.source for u in todo), *(u.target for u in todo)})
    cost = {(s, t): c for s, t, c in conn.execute(
        "SELECT start_vid, end_vid, agg_cost FROM pgr_dijkstraCostMatrix(%s, %s::bigint[], directed => true)",
        (EDGES_SQL, vids))}

    def dist(s: int, t: int) -> float:
        return 0.0 if s == t else cost.get((s, t), math.inf)

    # ponytail: greedy nearest-neighbour order, often 10-25% over optimal on
    # scattered picks; add 2-opt with direction flips if routes look loopy.
    order: list[tuple[Unit, bool]] = []   # (unit, forwards)
    at, left = home, list(todo)
    while left:
        u, fwd = min(((u, f) for u in left for f in (True, False)),
                     key=lambda uf: dist(at, uf[0].source if uf[1] else uf[0].target))
        left.remove(u)
        order.append((u, fwd))
        at = u.target if fwd else u.source

    def enter(i: int) -> int:
        u, fwd = order[i]
        return u.source if fwd else u.target

    def leave(i: int) -> int:
        u, fwd = order[i]
        return u.target if fwd else u.source

    # Deadheads between units, junction to junction, in one pgr_dijkstra call.
    pairs = {(leave(i - 1), enter(i)) for i in range(1, len(order))} - {(v, v) for v in vids}
    paths: dict[tuple[int, int], list[tuple[int, int]]] = {}
    if pairs:
        # Combinations SQL can't take parameters; these are vertex ids we read.
        combos = ", ".join(f"({int(s)}, {int(t)})" for s, t in sorted(pairs))
        for s, t, node, edge in conn.execute(
            "SELECT start_vid, end_vid, node, edge FROM pgr_dijkstra(%s, %s, directed => true)"
            " ORDER BY seq",
            (EDGES_SQL, f"SELECT * FROM (VALUES {combos}) AS c(source, target)"),
        ):
            if edge != -1:
                paths.setdefault((s, t), []).append((edge, node))

    # Each leg: its deadhead, then the unit, as (edge id, vertex it's walked from).
    # Units in the start's component are always connected, so every deadhead exists.
    flat = []
    for i, (u, fwd) in enumerate(order):
        steps = [] if i == 0 else list(paths.get((leave(i - 1), enter(i)), []))
        steps += [(e, s) for e, s, _ in u.edges] if fwd else [(e, t) for e, _, t in reversed(u.edges)]
        flat += [(i, e, v) for e, v in steps]
    geoms = conn.execute("""
        SELECT s.leg, ST_AsGeoJSON(ST_Transform(ST_RemoveRepeatedPoints(ST_MakeLine(
                   CASE WHEN s.walk_from = e.source THEN e.geom ELSE ST_Reverse(e.geom) END
                   ORDER BY s.ord)), 4326), 7)::json,
               sum(e.length_m)
        FROM unnest(%(legs)s::int[], %(edges)s::bigint[], %(walk_from)s::bigint[])
             WITH ORDINALITY AS s(leg, edge, walk_from, ord)
        JOIN route_edge e ON e.id = s.edge
        GROUP BY s.leg ORDER BY s.leg
    """, {
        "legs": [i for i, _, _ in flat],
        "edges": [e for _, e, _ in flat],
        "walk_from": [v for _, _, v in flat],
    }).fetchall()

    legs = []
    for _, gj, length_m in geoms:
        coords = gj["coordinates"] if gj["type"] == "LineString" else [gj["coordinates"]]
        legs.append({"latlngs": [(lat, lon) for lon, lat in coords], "length_m": length_m})

    # The first and last legs run from and back to the exact click.
    first = leg(conn, start, _vertex_latlng(conn, enter(0)), max_m)
    legs[0] = {"latlngs": first.latlngs + legs[0]["latlngs"][1:],
               "length_m": first.length_m + legs[0]["length_m"]}
    back = leg(conn, _vertex_latlng(conn, leave(len(order) - 1)), start, max_m)
    if back.length_m > 0:
        legs.append({"latlngs": back.latlngs, "length_m": back.length_m})
    for lg in legs:
        lg["to"] = lg["latlngs"][-1]

    return Finish(
        start=(a.lat, a.lon),
        legs=legs,
        length_m=sum(lg["length_m"] for lg in legs),
        skipped=len(found) - len(todo),
    )
