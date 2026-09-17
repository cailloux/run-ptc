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

from app.routing import RouteError, edges_sql, leg, network_row, snap

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


def new_miles(conn: psycopg.Connection, latlngs: list[LatLng], network: str = "ptc") -> NewMiles:
    """How much of a route runs over not-run intervals, each stretch counted once.

    The route is sampled every SAMPLE_M. A sample on an edge is new when the
    interval under it isn't run and no earlier sample passed the same spot of
    that part, so out-and-backs and retraces count once.
    """
    if len(latlngs) < 2:
        return NewMiles(0.0, [])
    network_id, srid = network_row(conn, network)
    rows = conn.execute(f"""
        WITH line AS (
            SELECT ST_Transform(ST_SetSRID(ST_MakeLine(ST_MakePoint(lon, lat) ORDER BY i), 4326), {srid}) AS geom
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
                WHERE network_id = %(network_id)s AND ST_DWithin(geom, s.pt, %(on_edge_m)s)
                ORDER BY geom <-> s.pt LIMIT 1
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
        "sample_m": SAMPLE_M, "on_edge_m": ON_EDGE_M, "network_id": network_id,
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


def units(conn: psycopg.Connection, segment_ids: list[int], network: str = "ptc") -> list[Unit]:
    """Edges of these segments that overlap a not-run interval, chained along each part.

    ponytail: a required edge is run end to end, even when only its far
    stretch is unrun; an out-and-back into part of an edge could be shorter.
    """
    network_id, _ = network_row(conn, network)
    rows = conn.execute(f"""
        WITH iv AS ({NOT_RUN_SQL.format(segments="(SELECT unnest(%(segments)s::bigint[]))")})
        SELECT DISTINCT e.id, e.segment_id, e.part_idx, e.part_from, e.source, e.target,
               v.component
        FROM route_edge e
        JOIN route_vertex v ON v.id = e.source AND v.network_id = e.network_id
        JOIN iv ON iv.segment_id = e.segment_id AND iv.part_idx = e.part_idx
               AND least(e.part_to, iv.b) - greatest(e.part_from, iv.a) > 1e-9
        WHERE e.network_id = %(network_id)s
        ORDER BY e.segment_id, e.part_idx, e.part_from
    """, {"segments": segment_ids, "network_id": network_id}).fetchall()
    chains: list[Unit] = []
    prev_part = None
    for edge_id, segment_id, part_idx, _, source, target, component in rows:
        if chains and prev_part == (segment_id, part_idx) and chains[-1].target == source:
            u = chains[-1]
            u.edges.append((edge_id, source, target))
            u.target = target
        else:
            chains.append(Unit([(edge_id, source, target)], source, target, component))
        prev_part = (segment_id, part_idx)

    # A vertex where some other unit starts or ends is a junction worth
    # stopping at: split any chain there too, so the router can detour to
    # that unit and back without re-walking the rest of this one to get
    # "official" credit for it (it was forced to walk to the junction
    # anyway to make the detour, so nothing extra is required by stopping).
    attach = {v for u in chains for v in (u.source, u.target)}
    out: list[Unit] = []
    for u in chains:
        own = {u.source, u.target}
        piece, source = [u.edges[0]], u.source
        for edge_id, s, t in u.edges[1:]:
            if s in attach and s not in own:
                out.append(Unit(piece, source, s, u.component))
                piece, source = [], s
            piece.append((edge_id, s, t))
        out.append(Unit(piece, source, u.target, u.component))
    return out


def _enter(step: tuple[Unit, bool]) -> int:
    u, fwd = step
    return u.source if fwd else u.target


def _leave(step: tuple[Unit, bool]) -> int:
    u, fwd = step
    return u.target if fwd else u.source


def _two_opt(order: list[tuple[Unit, bool]], home: int, dist) -> bool:
    """Improve a round trip in place by reversing runs of it.

    Reversing a run also flips each unit in it, so a run of one is a
    direction flip. Deadheads cost the same both ways (cost = reverse_cost),
    so only the two deadheads at the run's ends change. On real picks this
    matched the exact optimum (Held-Karp) in most trials and was within 2%
    in the rest, where greedy alone was up to 30% over.
    """
    changed = False
    improved = True
    while improved:
        improved = False
        for i in range(len(order)):
            before = home if i == 0 else _leave(order[i - 1])
            for j in range(i, len(order)):
                after = home if j == len(order) - 1 else _enter(order[j + 1])
                old = dist(before, _enter(order[i])) + dist(_leave(order[j]), after)
                new = dist(before, _leave(order[j])) + dist(_enter(order[i]), after)
                if new < old - 1e-6:
                    order[i:j + 1] = [(u, not f) for u, f in reversed(order[i:j + 1])]
                    improved = changed = True
    return changed


def _or_opt(order: list[tuple[Unit, bool]], home: int, dist) -> bool:
    """Improve a round trip by moving one unit to a better spot in it.

    2-opt only reverses whole runs, so it can't move a single stop past a
    same-cost detour to interleave it, which is exactly the shape a branch
    off the middle of a required street produces. Try every unit at every
    other position and direction, scored by the change in cost (removing
    it from where it is, plus inserting it at the candidate gap) rather
    than a full re-sum, so a pass is O(units^2) like 2-opt. Take the first
    move that's cheaper.
    """
    changed = False
    improved = True
    while improved:
        improved = False
        for i in range(len(order)):
            before = home if i == 0 else _leave(order[i - 1])
            after = home if i == len(order) - 1 else _enter(order[i + 1])
            removed = dist(before, after) - dist(before, _enter(order[i])) - dist(_leave(order[i]), after)
            u = order[i][0]
            rest = order[:i] + order[i + 1:]
            for j in range(len(rest) + 1):
                b = home if j == 0 else _leave(rest[j - 1])
                a = home if j == len(rest) else _enter(rest[j])
                base = dist(b, a)
                for fwd in (True, False):
                    added = dist(b, _enter((u, fwd))) + dist(_leave((u, fwd)), a) - base
                    if removed + added < -1e-6:
                        order[:] = rest[:j] + [(u, fwd)] + rest[j:]
                        improved = changed = True
                        break
                if improved:
                    break
            if improved:
                break
    return changed


def _vertex_latlng(conn: psycopg.Connection, vid: int, network_id: int) -> LatLng:
    return conn.execute(
        "SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(geom, 4326) p FROM route_vertex"
        " WHERE id = %s AND network_id = %s) v",
        (vid, network_id),
    ).fetchone()


def finish_route(conn: psycopg.Connection, start: LatLng, segment_ids: list[int],
                 max_m: float, network: str = "ptc") -> Finish:
    """A loop from start that runs every not-run stretch of these segments."""
    network_id, _ = network_row(conn, network)
    a = snap(conn, *start, max_m, network)
    found = units(conn, segment_ids, network)
    todo = [u for u in found if u.component == a.component]
    if not todo:
        raise RouteError("those segments can't be reached from the start (they're on an island)"
                         if found else "nothing left to run on those segments")

    # For ordering, the start counts as the nearer end of its edge.
    source, target = conn.execute(
        "SELECT source, target FROM route_edge WHERE id = %s AND network_id = %s",
        (a.edge_id, network_id)).fetchone()
    home = source if a.fraction < 0.5 else target
    vids = sorted({home, *(u.source for u in todo), *(u.target for u in todo)})
    cost = {(s, t): c for s, t, c in conn.execute(
        "SELECT start_vid, end_vid, agg_cost FROM pgr_dijkstraCostMatrix(%s, %s::bigint[], directed => true)",
        (edges_sql(network_id), vids))}

    def dist(s: int, t: int) -> float:
        return 0.0 if s == t else cost.get((s, t), math.inf)

    # Greedy nearest-neighbour order, then 2-opt and or-opt until neither improves.
    order: list[tuple[Unit, bool]] = []   # (unit, forwards)
    at, left = home, list(todo)
    while left:
        u, fwd = min(((u, f) for u in left for f in (True, False)),
                     key=lambda uf: dist(at, _enter(uf)))
        left.remove(u)
        order.append((u, fwd))
        at = _leave((u, fwd))
    while _two_opt(order, home, dist) | _or_opt(order, home, dist):
        pass

    def enter(i: int) -> int:
        return _enter(order[i])

    def leave(i: int) -> int:
        return _leave(order[i])

    # Deadheads between units, junction to junction, in one pgr_dijkstra call.
    pairs = {(leave(i - 1), enter(i)) for i in range(1, len(order))} - {(v, v) for v in vids}
    paths: dict[tuple[int, int], list[tuple[int, int]]] = {}
    if pairs:
        # Combinations SQL can't take parameters; these are vertex ids we read.
        combos = ", ".join(f"({int(s)}, {int(t)})" for s, t in sorted(pairs))
        for s, t, node, edge in conn.execute(
            "SELECT start_vid, end_vid, node, edge FROM pgr_dijkstra(%s, %s, directed => true)"
            " ORDER BY seq",
            (edges_sql(network_id), f"SELECT * FROM (VALUES {combos}) AS c(source, target)"),
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
        JOIN route_edge e ON e.id = s.edge AND e.network_id = %(network_id)s
        GROUP BY s.leg ORDER BY s.leg
    """, {
        "legs": [i for i, _, _ in flat],
        "edges": [e for _, e, _ in flat],
        "walk_from": [v for _, _, v in flat],
        "network_id": network_id,
    }).fetchall()

    legs = []
    for _, gj, length_m in geoms:
        coords = gj["coordinates"] if gj["type"] == "LineString" else [gj["coordinates"]]
        legs.append({"latlngs": [(lat, lon) for lon, lat in coords], "length_m": length_m})

    # The first and last legs run from and back to the exact click.
    first = leg(conn, start, _vertex_latlng(conn, enter(0), network_id), max_m, network)
    legs[0] = {"latlngs": first.latlngs + legs[0]["latlngs"][1:],
               "length_m": first.length_m + legs[0]["length_m"]}
    back = leg(conn, _vertex_latlng(conn, leave(len(order) - 1), network_id), start, max_m, network)
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
