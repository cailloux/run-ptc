"""Build the routing graph from every segment.

Rules are in docs/PLAN.md under "Routing graph". pgRouting 4.0 has no
topology builder that fits them (pgr_separateTouching fails on this data and
can't tell a tunnel from an at-grade crossing), so two steps are our own
SQL: splitting lines at T-junctions and at-grade crossings, and snapping
nearby ends. pgRouting assigns the junctions and finds the islands.
"""

from dataclasses import dataclass, field

import psycopg

from app.config import Settings
from app.matching import METERS_PER_MILE

# A road edge is "alongside a path" when this share of it lies within its
# match radius of a cart path.
PARALLEL_SHARE = 0.8


@dataclass
class Island:
    component: int
    length_m: float
    segments: list[str]   # "layer:oid"


@dataclass
class GraphReport:
    edges: int = 0
    vertices: int = 0
    components: int = 0
    main_m: float = 0.0
    alongside_edges: int = 0
    alongside_m: float = 0.0
    islands: list[Island] = field(default_factory=list)

    def lines(self) -> list[str]:
        island_m = sum(i.length_m for i in self.islands)
        lines = [
            f"graph: {self.edges} edges, {self.vertices} junctions, {self.components} components; "
            f"main network {self.main_m / METERS_PER_MILE:.2f} mi, "
            f"{len(self.islands)} islands ({island_m / METERS_PER_MILE:.2f} mi)",
            f"  {self.alongside_edges} road edges ({self.alongside_m / METERS_PER_MILE:.2f} mi) "
            f"have a cart path alongside; routing prefers the path",
        ]
        for i in self.islands:
            lines.append(f"  island {i.component}: {i.length_m:.0f} m, {', '.join(i.segments)}")
        return lines


def _network_id(conn: psycopg.Connection, network: str) -> int:
    row = conn.execute("SELECT id FROM network WHERE slug = %s", (network,)).fetchone()
    if row is None:
        raise RuntimeError(f"no network with slug {network!r}")
    return row[0]


def build_graph(conn: psycopg.Connection, settings: Settings, network: str = "ptc") -> GraphReport:
    """Replace this network's route_edge and route_vertex from its segments."""
    snap = settings.graph_snap_m
    with conn.transaction():
        network_id = _network_id(conn, network)
        for t in ("g_part", "g_end", "g_cut", "g_piece", "g_piece_end", "g_vertex"):
            conn.execute(f"DROP TABLE IF EXISTS pg_temp.{t}")

        # 1. Every part of every segment, as its own line.
        conn.execute("""
            CREATE TEMP TABLE g_part ON COMMIT DROP AS
            SELECT row_number() OVER () AS pid, s.id AS segment_id, s.layer, s.seg_type,
                   d.path[1] - 1 AS part_idx, d.geom
            FROM segment s CROSS JOIN LATERAL ST_Dump(s.geom) d
            WHERE s.network_id = %(network_id)s AND ST_Length(d.geom) >= %(min_part)s
        """, {"network_id": network_id, "min_part": settings.min_part_length_m})
        conn.execute("CREATE INDEX ON g_part USING gist (geom)")
        conn.execute("""
            CREATE TEMP TABLE g_end ON COMMIT DROP AS
            SELECT p.pid, p.layer, e.pt FROM g_part p
            CROSS JOIN LATERAL (VALUES (ST_StartPoint(p.geom)), (ST_EndPoint(p.geom))) e(pt)
        """)
        conn.execute("ANALYZE g_part")

        # 2. T-junctions: where a part's end lands on another part's middle,
        #    split that part there.
        conn.execute("""
            CREATE TEMP TABLE g_cut ON COMMIT DROP AS
            SELECT DISTINCT p.pid, round(ST_LineLocatePoint(p.geom, e.pt)::numeric, 6)::float8 AS f
            FROM g_end e JOIN g_part p ON p.pid <> e.pid AND ST_DWithin(e.pt, p.geom, %(snap)s)
            WHERE ST_Distance(e.pt, ST_StartPoint(p.geom)) > %(snap)s
              AND ST_Distance(e.pt, ST_EndPoint(p.geom)) > %(snap)s
              -- A road ending on a tunnel or bridge is above or below it.
              AND NOT (e.layer = 'road' AND p.seg_type IN ('Tunnel', 'Bridge'))
        """, {"snap": snap})
        #    At-grade crossings join too: both lines split where they cross,
        #    unless either is a tunnel or bridge (one passes over the other).
        #    A line whose own end is already at the crossing isn't split.
        conn.execute("""
            INSERT INTO g_cut (pid, f)
            SELECT DISTINCT q.pid, round(ST_LineLocatePoint(q.geom, x.pt)::numeric, 6)::float8
            FROM (
                SELECT a.pid AS pa, b.pid AS pb, (ST_Dump(ST_Intersection(a.geom, b.geom))).geom AS pt
                FROM g_part a JOIN g_part b ON a.pid < b.pid AND ST_Crosses(a.geom, b.geom)
                WHERE coalesce(a.seg_type, '') NOT IN ('Tunnel', 'Bridge')
                  AND coalesce(b.seg_type, '') NOT IN ('Tunnel', 'Bridge')
            ) x
            JOIN g_part q ON q.pid IN (x.pa, x.pb)
            WHERE GeometryType(x.pt) = 'POINT'
              AND ST_Distance(x.pt, ST_StartPoint(q.geom)) > %(snap)s
              AND ST_Distance(x.pt, ST_EndPoint(q.geom)) > %(snap)s
        """, {"snap": snap})
        conn.execute("""
            CREATE TEMP TABLE g_piece ON COMMIT DROP AS
            WITH bounds AS (
                SELECT pid, f, lead(f) OVER (PARTITION BY pid ORDER BY f) AS f_next
                FROM (SELECT pid, f FROM g_cut
                      UNION SELECT pid, 0 FROM g_part
                      UNION SELECT pid, 1 FROM g_part) b
            )
            SELECT row_number() OVER () AS id, p.segment_id, p.part_idx,
                   b.f AS part_from, b.f_next AS part_to,
                   ST_LineSubstring(p.geom, b.f, b.f_next) AS geom
            FROM bounds b JOIN g_part p USING (pid)
            WHERE b.f_next > b.f
        """)

        # 3. Snap ends within the tolerance onto one shared point, so routes
        #    have no gaps at junctions. pgRouting 4.0 has no snapping of its own.
        conn.execute("""
            CREATE TEMP TABLE g_piece_end ON COMMIT DROP AS
            SELECT id, which, pt, ST_ClusterDBSCAN(pt, %s, 1) OVER () AS cluster
            FROM (SELECT id, 's' AS which, ST_StartPoint(geom) AS pt FROM g_piece
                  UNION ALL SELECT id, 'e', ST_EndPoint(geom) FROM g_piece) x
        """, (snap,))

        conn.execute("DELETE FROM route_edge WHERE network_id = %(network_id)s",
                     {"network_id": network_id})
        conn.execute("DELETE FROM route_vertex WHERE network_id = %(network_id)s",
                     {"network_id": network_id})
        # Self-loops shorter than the tolerance are slivers that collapsed
        # onto one junction; longer loops (a path around a pond) stay.
        conn.execute("""
            WITH center AS (
                SELECT cluster, ST_Centroid(ST_Collect(pt)) AS pt FROM g_piece_end GROUP BY cluster
            ), snapped AS (
                SELECT p.id, p.segment_id, p.part_idx, p.part_from, p.part_to,
                       s.cluster = e.cluster AS loop,
                       ST_SetPoint(ST_SetPoint(p.geom, 0, cs.pt), -1, ce.pt) AS geom
                FROM g_piece p
                JOIN g_piece_end s ON s.id = p.id AND s.which = 's' JOIN center cs ON cs.cluster = s.cluster
                JOIN g_piece_end e ON e.id = p.id AND e.which = 'e' JOIN center ce ON ce.cluster = e.cluster
            )
            INSERT INTO route_edge (network_id, id, length_m, cost, reverse_cost, segment_id,
                                    part_idx, part_from, part_to, geom)
            SELECT %(network_id)s, id, ST_Length(geom), ST_Length(geom), ST_Length(geom),
                   segment_id, part_idx, part_from, part_to, geom
            FROM snapped
            WHERE NOT (loop AND ST_Length(geom) < %(snap)s)
        """, {"snap": snap, "network_id": network_id})

        # Prefer cart paths: a road stretch with a path alongside it, within
        # the road's own match radius (so running the path still completes
        # the road), costs more. length_m keeps the true length.
        conn.execute("""
            WITH road AS (
                SELECT e.id, e.geom, e.length_m,
                       CASE WHEN s.seg_type = ANY(%(wide_classes)s) OR s.name = ANY(%(divided)s)
                            THEN %(wide_m)s ELSE %(road_m)s END AS radius
                FROM route_edge e JOIN segment s ON s.id = e.segment_id
                WHERE s.layer = 'road' AND e.network_id = %(network_id)s
            ), sample AS (
                -- Points every 5 m along each road edge. (Measuring the share
                -- this way, with an indexed EXISTS per point, is ~100x faster
                -- than share_within() against a collection of nearby paths.)
                SELECT r.id, r.radius, ST_LineInterpolatePoint(r.geom, i::float8 / k.n) AS pt
                FROM road r
                CROSS JOIN LATERAL (SELECT GREATEST(1, CEIL(r.length_m / 5))::int AS n) k
                CROSS JOIN LATERAL generate_series(0, k.n) i
            ), alongside AS (
                SELECT s.id
                FROM sample s
                GROUP BY s.id
                HAVING avg(CASE WHEN EXISTS (
                    SELECT 1 FROM g_part p
                    WHERE p.layer = 'cartpath' AND coalesce(p.seg_type, '') NOT IN ('Tunnel', 'Bridge')
                      AND ST_DWithin(p.geom, s.pt, s.radius)
                ) THEN 1.0 ELSE 0.0 END) >= %(share)s
            )
            UPDATE route_edge e SET cost = e.length_m * %(factor)s, reverse_cost = e.length_m * %(factor)s
            FROM alongside a WHERE a.id = e.id AND e.network_id = %(network_id)s
        """, {
            "wide_classes": list(settings.match_radius_wide_road_classes),
            "divided": list(settings.divided_roads),
            "wide_m": settings.match_radius_wide_m,
            "road_m": settings.match_radius_road_m,
            "share": PARALLEL_SHARE,
            "factor": settings.route_parallel_road_factor,
            "network_id": network_id,
        })

        # 4. Junctions and edge ends, from pgRouting. network_id is our own
        # resolved integer, safe to interpolate into pgRouting's SQL-text arg.
        conn.execute(f"""
            CREATE TEMP TABLE g_vertex ON COMMIT DROP AS
            SELECT id, in_edges, out_edges, geom
            FROM pgr_extractVertices('SELECT id, geom FROM route_edge WHERE network_id = {network_id}')
        """)
        conn.execute("INSERT INTO route_vertex (network_id, id, geom) SELECT %(network_id)s, id, geom FROM g_vertex",
                     {"network_id": network_id})
        conn.execute("""
            WITH src AS (SELECT unnest(out_edges) AS edge, id FROM g_vertex),
                 tgt AS (SELECT unnest(in_edges) AS edge, id FROM g_vertex)
            UPDATE route_edge e SET source = src.id, target = tgt.id
            FROM src, tgt WHERE src.edge = e.id AND tgt.edge = e.id AND e.network_id = %(network_id)s
        """, {"network_id": network_id})

        # 5. Islands, from pgRouting.
        conn.execute(f"""
            UPDATE route_vertex v SET component = c.component
            FROM pgr_connectedComponents(
                'SELECT id, source, target, cost, reverse_cost FROM route_edge WHERE network_id = {network_id}') c
            WHERE c.node = v.id AND v.network_id = {network_id}
        """)
        return graph_report(conn, network)


def graph_report(conn: psycopg.Connection, network: str = "ptc") -> GraphReport:
    network_id = _network_id(conn, network)
    report = GraphReport()
    (report.edges, report.vertices, report.components,
     report.alongside_edges, report.alongside_m) = conn.execute("""
        SELECT (SELECT count(*) FROM route_edge WHERE network_id = %(network_id)s),
               (SELECT count(*) FROM route_vertex WHERE network_id = %(network_id)s),
               (SELECT count(DISTINCT component) FROM route_vertex WHERE network_id = %(network_id)s),
               (SELECT count(*) FROM route_edge WHERE network_id = %(network_id)s AND cost > length_m),
               (SELECT coalesce(sum(length_m), 0) FROM route_edge
                WHERE network_id = %(network_id)s AND cost > length_m)
    """, {"network_id": network_id}).fetchone()
    rows = conn.execute("""
        SELECT v.component, sum(e.length_m) AS length_m,
               array_agg(DISTINCT s.layer || ':' || s.source_oid ORDER BY s.layer || ':' || s.source_oid)
        FROM route_edge e
        JOIN route_vertex v ON v.id = e.source AND v.network_id = e.network_id
        JOIN segment s ON s.id = e.segment_id
        WHERE e.network_id = %(network_id)s
        GROUP BY v.component ORDER BY length_m DESC
    """, {"network_id": network_id}).fetchall()
    if rows:
        report.main_m = rows[0][1]
        report.islands = [Island(c, m, segs) for c, m, segs in rows[1:]]
    return report


def main_component(conn: psycopg.Connection, network: str = "ptc") -> int | None:
    network_id = _network_id(conn, network)
    row = conn.execute("""
        SELECT v.component FROM route_edge e
        JOIN route_vertex v ON v.id = e.source AND v.network_id = e.network_id
        WHERE e.network_id = %(network_id)s
        GROUP BY v.component ORDER BY sum(e.length_m) DESC LIMIT 1
    """, {"network_id": network_id}).fetchone()
    return row[0] if row else None
