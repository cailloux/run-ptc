"""Node hits, the tunnel rule, completion metrics, and recompute.

Rules are in docs/PLAN.md under "Matching". A node is credited to the
earliest run (by start time) whose track passes within its radius, so
matching a late-synced older run replaces a newer run's hit, and matching a
newer run never replaces an older one.
"""

from dataclasses import dataclass

import psycopg

from app.config import Settings

METERS_PER_MILE = 1609.344
# Track piece length for matching. Short pieces keep the spatial index useful.
PIECE_M = 200


def rebuild_pieces(conn: psycopg.Connection, activity_ids: list[int] | None = None) -> None:
    """Recut city tracks into pieces (all of them when activity_ids is None)."""
    params = {"ids": activity_ids, "piece_m": PIECE_M}
    conn.execute(
        "DELETE FROM activity_piece WHERE %(ids)s::bigint[] IS NULL OR activity_id = ANY(%(ids)s)",
        params,
    )
    conn.execute("""
        INSERT INTO activity_piece (activity_id, start_at, geom)
        SELECT a.id, a.start_at, p
        FROM activity a CROSS JOIN LATERAL track_pieces(a.geom, %(piece_m)s) AS p
        WHERE a.status = 'city' AND (%(ids)s::bigint[] IS NULL OR a.id = ANY(%(ids)s))
    """, params)


def assign_radii(conn: psycopg.Connection, settings: Settings, *,
                 segment_ids: list[int] | None = None, missing_only: bool = False) -> None:
    conn.execute("""
        UPDATE node n
        SET radius_m = CASE
            WHEN s.layer = 'cartpath' THEN %(cartpath_m)s
            WHEN s.seg_type = ANY(%(wide_classes)s) THEN %(wide_m)s
            ELSE %(road_m)s
        END
        FROM segment s
        WHERE s.id = n.segment_id
          AND (%(ids)s::bigint[] IS NULL OR s.id = ANY(%(ids)s))
          AND (NOT %(missing_only)s OR n.radius_m IS NULL)
    """, {
        "wide_classes": list(settings.match_radius_wide_road_classes),
        "cartpath_m": settings.match_radius_cartpath_m,
        "road_m": settings.match_radius_road_m,
        "wide_m": settings.match_radius_wide_m,
        "ids": segment_ids,
        "missing_only": missing_only,
    })


# Earlier run wins; ties go to the lower activity id.
_EARLIER = "(n.hit_at IS NULL OR (n.hit_at, n.hit_activity_id) > (c.start_at, c.activity_id))"

# A few runs: start from their pieces and probe the node index.
_DIRECT_FROM_RUNS = f"""
WITH p AS MATERIALIZED (
    SELECT activity_id, start_at, geom FROM activity_piece WHERE activity_id = ANY(%(acts)s)
), c AS (
    SELECT DISTINCT ON (n.id) n.id AS node_id, p.activity_id, p.start_at
    FROM p JOIN node n ON ST_DWithin(n.geom, p.geom, %(max_r)s)
    WHERE ST_DWithin(n.geom, p.geom, n.radius_m)
      AND (%(segs)s::bigint[] IS NULL OR n.segment_id = ANY(%(segs)s))
    ORDER BY n.id, p.start_at, p.activity_id
)
UPDATE node n SET hit_activity_id = c.activity_id, hit_at = c.start_at
FROM c WHERE n.id = c.node_id AND {_EARLIER}
"""

# Every run: start from the nodes and probe the piece index.
_DIRECT_FROM_NODES = f"""
WITH c AS (
    SELECT DISTINCT ON (n.id) n.id AS node_id, p.activity_id, p.start_at
    FROM node n JOIN activity_piece p ON ST_DWithin(n.geom, p.geom, n.radius_m)
    WHERE %(segs)s::bigint[] IS NULL OR n.segment_id = ANY(%(segs)s)
    ORDER BY n.id, p.start_at, p.activity_id
)
UPDATE node n SET hit_activity_id = c.activity_id, hit_at = c.start_at
FROM c WHERE n.id = c.node_id AND {_EARLIER}
"""

# A run that hits both end nodes of a tunnel is credited with the whole
# tunnel, since GPS usually drops out underground.
_TUNNELS = f"""
WITH tunnel AS (
    SELECT id FROM segment
    WHERE counted AND seg_type = 'Tunnel'
      AND (%(segs)s::bigint[] IS NULL OR id = ANY(%(segs)s))
), ends AS (
    SELECT t.id AS segment_id, e.is_first, n.geom, n.radius_m
    FROM tunnel t
    CROSS JOIN LATERAL (
        (SELECT id, true AS is_first FROM node WHERE segment_id = t.id
         ORDER BY part_idx, seq LIMIT 1)
        UNION ALL
        (SELECT id, false FROM node WHERE segment_id = t.id
         ORDER BY part_idx DESC, seq DESC LIMIT 1)
    ) e
    JOIN node n ON n.id = e.id
), both_ends AS (
    SELECT e.segment_id, p.activity_id, min(p.start_at) AS start_at
    FROM ends e JOIN activity_piece p ON ST_DWithin(e.geom, p.geom, e.radius_m)
    WHERE %(acts)s::bigint[] IS NULL OR p.activity_id = ANY(%(acts)s)
    GROUP BY e.segment_id, p.activity_id
    HAVING count(DISTINCT e.is_first) = 2
), c AS (
    SELECT DISTINCT ON (segment_id) segment_id, activity_id, start_at
    FROM both_ends ORDER BY segment_id, start_at, activity_id
)
UPDATE node n SET hit_activity_id = c.activity_id, hit_at = c.start_at
FROM c WHERE n.segment_id = c.segment_id AND {_EARLIER}
"""


def _hit_count(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) FROM node WHERE hit_at IS NOT NULL").fetchone()[0]


def match(conn: psycopg.Connection, settings: Settings, *,
          activity_ids: list[int] | None = None, segment_ids: list[int] | None = None) -> int:
    """Credit nodes to runs; None means all runs / all segments. Returns nodes newly hit."""
    if activity_ids == [] or segment_ids == []:
        return 0
    before = _hit_count(conn)
    with conn.transaction():
        assign_radii(conn, settings, missing_only=True)
        params = {
            "acts": activity_ids,
            "segs": segment_ids,
            "max_r": max(settings.match_radius_cartpath_m, settings.match_radius_road_m,
                         settings.match_radius_wide_m),
        }
        conn.execute(_DIRECT_FROM_NODES if activity_ids is None else _DIRECT_FROM_RUNS, params)
        conn.execute(_TUNNELS, params)
    return _hit_count(conn) - before


def recompute(conn: psycopg.Connection, settings: Settings) -> int:
    """Re-split tracks, reassign radii, clear every hit, and replay every run."""
    with conn.transaction():
        conn.execute(
            "UPDATE activity SET geom = split_track(track_raw, %s) WHERE status = 'city'",
            (settings.track_gap_split_m,),
        )
        rebuild_pieces(conn)
        assign_radii(conn, settings)
        conn.execute("UPDATE node SET hit_activity_id = NULL, hit_at = NULL WHERE hit_at IS NOT NULL")
        return match(conn, settings)


@dataclass
class Metrics:
    cartpath_complete_m: float
    cartpath_total_m: float
    segments_complete: int
    segments_total: int
    road_covered_m: float
    road_total_m: float

    @property
    def cartpath_pct(self) -> float:
        return 100 * self.cartpath_complete_m / self.cartpath_total_m if self.cartpath_total_m else 0.0

    def as_dict(self) -> dict:
        def mi(m: float) -> float:
            return round(m / METERS_PER_MILE, 2)

        return {
            "cartpath_complete_mi": mi(self.cartpath_complete_m),
            "cartpath_total_mi": mi(self.cartpath_total_m),
            "cartpath_pct": round(self.cartpath_pct, 1),
            "segments_complete": self.segments_complete,
            "segments_total": self.segments_total,
            "road_covered_mi": mi(self.road_covered_m),
            "road_total_mi": mi(self.road_total_m),
        }

    def lines(self) -> list[str]:
        d = self.as_dict()
        return [
            f"cart path miles complete: {d['cartpath_complete_mi']} of {d['cartpath_total_mi']} mi "
            f"({d['cartpath_pct']}%)",
            f"segments complete: {d['segments_complete']} of {d['segments_total']}",
            f"road miles covered: {d['road_covered_mi']} of {d['road_total_mi']} mi",
        ]


def metrics(conn: psycopg.Connection) -> Metrics:
    """Counted, non-excluded segments only. A cart path is complete when every node is hit."""
    cart_complete_m, cart_total_m, complete, total, road_total_m = conn.execute("""
        WITH seg AS (
            SELECT s.layer, s.length_m, count(n.id) AS nodes, count(n.hit_at) AS hit
            FROM segment s LEFT JOIN node n ON n.segment_id = s.id
            WHERE s.counted AND NOT s.excluded
            GROUP BY s.id
        )
        SELECT coalesce(sum(length_m) FILTER (WHERE layer = 'cartpath' AND nodes > 0 AND hit = nodes), 0),
               coalesce(sum(length_m) FILTER (WHERE layer = 'cartpath'), 0),
               count(*) FILTER (WHERE layer = 'cartpath' AND nodes > 0 AND hit = nodes),
               count(*) FILTER (WHERE layer = 'cartpath'),
               coalesce(sum(length_m) FILTER (WHERE layer = 'road'), 0)
        FROM seg
    """).fetchone()
    # Road coverage counts node-to-node intervals with both ends hit, within
    # one part. Nodes are evenly spaced, so each interval is the part's
    # length divided by its interval count (the last seq).
    road_covered_m = conn.execute("""
        WITH nodes AS (
            SELECT n.segment_id, n.part_idx, n.seq, n.hit_at IS NOT NULL AS hit,
                   lag(n.hit_at IS NOT NULL) OVER (
                       PARTITION BY n.segment_id, n.part_idx ORDER BY n.seq) AS prev_hit
            FROM node n JOIN segment s ON s.id = n.segment_id
            WHERE s.layer = 'road' AND s.counted AND NOT s.excluded
        ), parts AS (
            SELECT segment_id, part_idx, max(seq) AS intervals,
                   count(*) FILTER (WHERE hit AND prev_hit) AS covered
            FROM nodes GROUP BY 1, 2
        )
        SELECT coalesce(sum(ST_Length(ST_GeometryN(s.geom, p.part_idx + 1)) * p.covered / p.intervals), 0)
        FROM parts p JOIN segment s ON s.id = p.segment_id
        WHERE p.intervals > 0
    """).fetchone()[0]
    return Metrics(cart_complete_m, cart_total_m, complete, total, road_covered_m, road_total_m)


NEAR_MISS_LIMITS_M = (10, 15, 20, 25, 30, 40, 60)
NEAR_MISS_BUCKETS = tuple(f"<{m} m" for m in NEAR_MISS_LIMITS_M) + ("farther",)


def near_misses(conn: psycopg.Connection) -> dict[str, dict[str, int]]:
    """Missed nodes by distance to the nearest city track, per layer. A tuning aid."""
    rows = conn.execute("""
        WITH missed AS (
            SELECT s.layer,
                   (SELECT min(ST_Distance(n.geom, p.geom)) FROM activity_piece p
                    WHERE ST_DWithin(n.geom, p.geom, %(far)s)) AS d
            FROM node n JOIN segment s ON s.id = n.segment_id
            WHERE s.counted AND NOT s.excluded AND n.hit_at IS NULL
        )
        -- width_bucket gives the index of the first limit >= d (1-based);
        -- NULL (nothing within the last limit) lands in "farther".
        SELECT layer, coalesce(width_bucket(d, %(limits)s::float8[]), %(n)s) AS bucket, count(*)
        FROM missed GROUP BY 1, 2
    """, {"far": NEAR_MISS_LIMITS_M[-1],
          "limits": [0.0, *map(float, NEAR_MISS_LIMITS_M)],
          "n": len(NEAR_MISS_LIMITS_M) + 1}).fetchall()
    result = {layer: dict.fromkeys(NEAR_MISS_BUCKETS, 0) for layer in ("cartpath", "road")}
    for layer, bucket, n in rows:
        result[layer][NEAR_MISS_BUCKETS[min(bucket, len(NEAR_MISS_BUCKETS)) - 1]] += n
    return result


def near_miss_lines(table: dict[str, dict[str, int]]) -> list[str]:
    lines = ["missed nodes by distance to the nearest run:",
             "  " + " " * 9 + "".join(f"{b:>9}" for b in NEAR_MISS_BUCKETS)]
    for layer, counts in table.items():
        lines.append(f"  {layer:<9}" + "".join(f"{counts[b]:>9}" for b in NEAR_MISS_BUCKETS))
    return lines
