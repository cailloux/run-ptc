"""Route legs over the graph, from the exact clicked points.

Each click snaps to the nearest edge (pgr_findCloseEdges), and a leg runs
from the end of the route to that point with pgr_withPoints, so it starts
and ends on the clicked spot rather than at the nearest junction.
"""

from dataclasses import dataclass

import psycopg

EDGES_SQL = "SELECT id, source, target, cost, reverse_cost FROM route_edge"


def point_expr(lon: str = "%(lon)s", lat: str = "%(lat)s") -> str:
    """SQL transforming a lon/lat pair (params or column refs) into the graph's SRID."""
    return f"ST_Transform(ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326), 32616)"


# A click's position in the graph's SRID.
_POINT = point_expr()


class RouteError(ValueError):
    """The request can't be routed (too far from the network, or unreachable)."""


class GraphMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Snap:
    edge_id: int
    fraction: float
    component: int
    lat: float
    lon: float


def snap(conn: psycopg.Connection, lat: float, lon: float, max_m: float) -> Snap:
    if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM route_edge)").fetchone()[0]:
        raise GraphMissing("the routing graph hasn't been built; run python -m app.cli graph")
    row = conn.execute(f"""
        SELECT c.edge_id, c.fraction, v.component,
               ST_Y(ST_Transform(ST_LineInterpolatePoint(e.geom, c.fraction), 4326)),
               ST_X(ST_Transform(ST_LineInterpolatePoint(e.geom, c.fraction), 4326))
        FROM pgr_findCloseEdges(%(edges)s, {_POINT}, %(max_m)s) c
        JOIN route_edge e ON e.id = c.edge_id
        JOIN route_vertex v ON v.id = e.source
        ORDER BY c.distance LIMIT 1
    """, {"edges": "SELECT id, geom FROM route_edge", "lat": lat, "lon": lon, "max_m": max_m}).fetchone()
    if row is None:
        raise RouteError(f"that spot is more than {max_m:g} m from any path or road")
    return Snap(*row)


@dataclass
class Leg:
    latlngs: list[tuple[float, float]]
    length_m: float
    start: Snap
    end: Snap


def leg(conn: psycopg.Connection, start: tuple[float, float], end: tuple[float, float],
        max_m: float) -> Leg:
    """Shortest path between two clicked points, starting and ending on them."""
    a, b = snap(conn, *start, max_m), snap(conn, *end, max_m)
    if a.component != b.component:
        raise RouteError("no path or road connects those two points (one of them is on an island)")
    if a.edge_id == b.edge_id and abs(a.fraction - b.fraction) < 1e-9:
        return Leg([(a.lat, a.lon)], 0.0, a, b)

    # Points SQL can't take parameters; these are an int and floats we produced.
    points_sql = (
        f"SELECT 1 AS pid, {int(a.edge_id)}::bigint AS edge_id, {float(a.fraction)!r}::float8 AS fraction "
        f"UNION ALL SELECT 2, {int(b.edge_id)}::bigint, {float(b.fraction)!r}::float8"
    )
    path = conn.execute(
        "SELECT seq, node, edge FROM pgr_withPoints(%s, %s, -1, -2, directed => true) ORDER BY seq",
        (EDGES_SQL, points_sql),
    ).fetchall()
    if not path:
        raise RouteError("no route found between those points")

    # Each step walks edge `edge` from `node` to the next row's node. Cut the
    # edge between those two positions (reversed when walked backwards) and
    # join the pieces. Point nodes are -1 (start) and -2 (end).
    seqs = [r[0] for r in path]
    nodes = [r[1] for r in path]
    edges = [r[2] for r in path]
    next_nodes = nodes[1:] + [None]
    row = conn.execute("""
        WITH pts AS (
            SELECT -1::bigint AS node, ST_LineInterpolatePoint(ea.geom, %(fa)s) AS pt
            FROM route_edge ea WHERE ea.id = %(ea)s
            UNION ALL
            SELECT -2, ST_LineInterpolatePoint(eb.geom, %(fb)s) FROM route_edge eb WHERE eb.id = %(eb)s
        ), steps AS (
            SELECT * FROM unnest(%(seqs)s::int[], %(nodes)s::bigint[], %(edges)s::bigint[],
                                 %(next)s::bigint[]) AS s(seq, node, edge, next_node)
            WHERE edge <> -1
        ), pos AS (
            SELECT s.seq, e.geom, s.node, s.next_node,
                   ST_LineLocatePoint(e.geom, coalesce(p1.pt, v1.geom)) AS f1,
                   ST_LineLocatePoint(e.geom, coalesce(p2.pt, v2.geom)) AS f2
            FROM steps s JOIN route_edge e ON e.id = s.edge
            LEFT JOIN pts p1 ON p1.node = s.node LEFT JOIN route_vertex v1 ON v1.id = s.node
            LEFT JOIN pts p2 ON p2.node = s.next_node LEFT JOIN route_vertex v2 ON v2.id = s.next_node
        ), pieces AS (
            SELECT seq, CASE
                -- A loop edge walked junction to junction is the whole loop.
                WHEN node = next_node AND node >= 0 THEN geom
                WHEN f1 <= f2 THEN ST_LineSubstring(geom, f1, f2)
                ELSE ST_Reverse(ST_LineSubstring(geom, f2, f1))
            END AS geom
            FROM pos
        ), line AS (
            SELECT ST_RemoveRepeatedPoints(ST_MakeLine(geom ORDER BY seq)) AS geom FROM pieces
        )
        SELECT ST_AsGeoJSON(ST_Transform(geom, 4326), 7)::json, ST_Length(geom) FROM line
    """, {"fa": a.fraction, "ea": a.edge_id, "fb": b.fraction, "eb": b.edge_id,
          "seqs": seqs, "nodes": nodes, "edges": edges, "next": next_nodes}).fetchone()
    geojson, length_m = row
    coords = geojson["coordinates"] if geojson["type"] == "LineString" else [geojson["coordinates"]]
    return Leg([(lat, lon) for lon, lat in coords], length_m, a, b)
