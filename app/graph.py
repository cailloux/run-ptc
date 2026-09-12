"""Build the routing graph from every segment.

Rules are in docs/PLAN.md under "Routing graph". pgRouting 4.0 has no
topology builder that fits them (pgr_separateTouching fails on this data and
can join lines where they only cross), so two steps are our own SQL:
splitting lines where another line ends on them, and snapping nearby ends.
pgRouting assigns the junctions and finds the islands.
"""

from dataclasses import dataclass, field

import psycopg

from app.config import Settings
from app.matching import METERS_PER_MILE


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
    islands: list[Island] = field(default_factory=list)

    def lines(self) -> list[str]:
        island_m = sum(i.length_m for i in self.islands)
        lines = [
            f"graph: {self.edges} edges, {self.vertices} junctions, {self.components} components; "
            f"main network {self.main_m / METERS_PER_MILE:.2f} mi, "
            f"{len(self.islands)} islands ({island_m / METERS_PER_MILE:.2f} mi)",
        ]
        for i in self.islands:
            lines.append(f"  island {i.component}: {i.length_m:.0f} m, {', '.join(i.segments)}")
        return lines


def build_graph(conn: psycopg.Connection, settings: Settings) -> GraphReport:
    """Replace route_edge and route_vertex from the current segments."""
    snap = settings.graph_snap_m
    with conn.transaction():
        for t in ("g_part", "g_end", "g_cut", "g_piece", "g_piece_end", "g_vertex"):
            conn.execute(f"DROP TABLE IF EXISTS pg_temp.{t}")

        # 1. Every part of every segment, as its own line.
        conn.execute("""
            CREATE TEMP TABLE g_part ON COMMIT DROP AS
            SELECT row_number() OVER () AS pid, s.id AS segment_id, s.layer, s.seg_type,
                   d.path[1] - 1 AS part_idx, d.geom
            FROM segment s CROSS JOIN LATERAL ST_Dump(s.geom) d
            WHERE ST_Length(d.geom) >= %s
        """, (settings.min_part_length_m,))
        conn.execute("CREATE INDEX ON g_part USING gist (geom)")
        conn.execute("""
            CREATE TEMP TABLE g_end ON COMMIT DROP AS
            SELECT p.pid, p.layer, e.pt FROM g_part p
            CROSS JOIN LATERAL (VALUES (ST_StartPoint(p.geom)), (ST_EndPoint(p.geom))) e(pt)
        """)
        conn.execute("ANALYZE g_part")

        # 2. T-junctions: where a part's end lands on another part's middle,
        #    split that part there. Only ends split lines; lines that merely
        #    cross (tunnels under roads, bridges over them) never join.
        conn.execute("""
            CREATE TEMP TABLE g_cut ON COMMIT DROP AS
            SELECT DISTINCT p.pid, round(ST_LineLocatePoint(p.geom, e.pt)::numeric, 6)::float8 AS f
            FROM g_end e JOIN g_part p ON p.pid <> e.pid AND ST_DWithin(e.pt, p.geom, %(snap)s)
            WHERE ST_Distance(e.pt, ST_StartPoint(p.geom)) > %(snap)s
              AND ST_Distance(e.pt, ST_EndPoint(p.geom)) > %(snap)s
              -- A road ending on a tunnel or bridge is above or below it.
              AND NOT (e.layer = 'road' AND p.seg_type IN ('Tunnel', 'Bridge'))
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

        conn.execute("DELETE FROM route_edge")
        conn.execute("DELETE FROM route_vertex")
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
            INSERT INTO route_edge (id, cost, reverse_cost, segment_id, part_idx, part_from, part_to, geom)
            SELECT id, ST_Length(geom), ST_Length(geom), segment_id, part_idx, part_from, part_to, geom
            FROM snapped
            WHERE NOT (loop AND ST_Length(geom) < %s)
        """, (snap,))

        # 4. Junctions and edge ends, from pgRouting.
        conn.execute("""
            CREATE TEMP TABLE g_vertex ON COMMIT DROP AS
            SELECT id, in_edges, out_edges, geom
            FROM pgr_extractVertices('SELECT id, geom FROM route_edge')
        """)
        conn.execute("INSERT INTO route_vertex (id, geom) SELECT id, geom FROM g_vertex")
        conn.execute("""
            WITH src AS (SELECT unnest(out_edges) AS edge, id FROM g_vertex),
                 tgt AS (SELECT unnest(in_edges) AS edge, id FROM g_vertex)
            UPDATE route_edge e SET source = src.id, target = tgt.id
            FROM src, tgt WHERE src.edge = e.id AND tgt.edge = e.id
        """)

        # 5. Islands, from pgRouting.
        conn.execute("""
            UPDATE route_vertex v SET component = c.component
            FROM pgr_connectedComponents(
                'SELECT id, source, target, cost, reverse_cost FROM route_edge') c
            WHERE c.node = v.id
        """)
        return graph_report(conn)


def graph_report(conn: psycopg.Connection) -> GraphReport:
    report = GraphReport()
    report.edges, report.vertices, report.components = conn.execute("""
        SELECT (SELECT count(*) FROM route_edge), (SELECT count(*) FROM route_vertex),
               (SELECT count(DISTINCT component) FROM route_vertex)
    """).fetchone()
    rows = conn.execute("""
        SELECT v.component, sum(e.cost) AS length_m,
               array_agg(DISTINCT s.layer || ':' || s.source_oid ORDER BY s.layer || ':' || s.source_oid)
        FROM route_edge e JOIN route_vertex v ON v.id = e.source JOIN segment s ON s.id = e.segment_id
        GROUP BY v.component ORDER BY length_m DESC
    """).fetchall()
    if rows:
        report.main_m = rows[0][1]
        report.islands = [Island(c, m, segs) for c, m, segs in rows[1:]]
    return report


def main_component(conn: psycopg.Connection) -> int | None:
    row = conn.execute("""
        SELECT v.component FROM route_edge e JOIN route_vertex v ON v.id = e.source
        GROUP BY v.component ORDER BY sum(e.cost) DESC LIMIT 1
    """).fetchone()
    return row[0] if row else None
