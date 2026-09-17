import dataclasses
import math
from datetime import date, datetime

import pytest

from app import exclusions as excl
from app.matching import match, metrics, near_misses, rebuild_pieces, recompute
from app.sync import sync
from tests.helpers import SETTINGS, X0, Y0, add_network, cartpath, global_id, line, network_id, road, run_import
from tests.test_sync import FakeIntervals, city, run, walk


def densify(waypoints, step=10.0):
    """Points every `step` m along the waypoints, so no gap exceeds the split threshold."""
    points = [waypoints[0]]
    for (x0, y0), (x1, y1) in zip(waypoints, waypoints[1:]):
        n = max(1, math.ceil(math.dist((x0, y0), (x1, y1)) / step))
        points += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(1, n + 1)]
    return points


def add_run(conn, intervals_id, *waypoints, start="2024-05-04T12:00:00+00:00", raw=False, network="ptc"):
    """Store a city run directly from UTM offsets, the way sync would."""
    points = list(waypoints) if raw else densify(waypoints)
    wkt = "LINESTRING(" + ", ".join(f"{X0 + x} {Y0 + y}" for x, y in points) + ")"
    activity_id = conn.execute("""
        INSERT INTO activity (intervals_id, start_at, sport, status, track_raw, geom, network_id)
        VALUES (%(id)s, %(start)s, 'Run', 'city', ST_Transform(ST_GeomFromText(%(wkt)s, 32616), 4326),
                split_track(ST_GeomFromText(%(wkt)s, 32616), %(gap)s), %(network_id)s)
        RETURNING id
    """, {"id": intervals_id, "start": datetime.fromisoformat(start), "wkt": wkt,
          "gap": SETTINGS.track_gap_split_m, "network_id": network_id(conn, network)}).fetchone()[0]
    rebuild_pieces(conn, [activity_id])
    return activity_id


def hits(conn, oid, layer="cartpath"):
    """Intervals id (or None) of the run credited on each node, in order along the segment."""
    return [iid for (iid,) in conn.execute("""
        SELECT a.intervals_id FROM node n
        JOIN segment s ON s.id = n.segment_id
        LEFT JOIN activity a ON a.id = n.hit_activity_id
        WHERE s.layer = %s AND s.source_oid = %s
        ORDER BY n.part_idx, n.seq
    """, (layer, oid))]


def radii(conn):
    return dict(conn.execute("""
        SELECT s.layer || ':' || s.source_oid, min(n.radius_m) FROM node n
        JOIN segment s ON s.id = n.segment_id GROUP BY 1
    """).fetchall())


# ---- radius -------------------------------------------------------------------


def test_radius_by_layer_and_road_class(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    run_import(conn, "road", [
        road(1, line((0, 100), (40, 100)), CLASS="Arterial"),
        road(2, line((0, 200), (40, 200))),
    ])
    assert radii(conn) == {"cartpath:1": 20, "road:1": 30, "road:2": 20}

    tuned = dataclasses.replace(SETTINGS, match_radius_cartpath_m=5, match_radius_road_m=15,
                                match_radius_wide_m=35)
    recompute(conn, tuned)
    assert radii(conn) == {"cartpath:1": 5, "road:1": 35, "road:2": 15}


def test_cartpath_end_nodes_get_their_own_radius_per_part(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (40, 0)), line((100, 0), (140, 0))),   # two parts, 3 nodes each
        cartpath(2, line((0, 50), (10, 50))),                             # 2 nodes, both ends
    ])
    recompute(conn, dataclasses.replace(SETTINGS, match_radius_cartpath_m=5,
                                        match_radius_cartpath_end_m=12))
    rows = conn.execute("""
        SELECT s.source_oid, n.part_idx, n.seq, n.radius_m FROM node n
        JOIN segment s ON s.id = n.segment_id ORDER BY 1, 2, 3
    """).fetchall()
    assert rows == [
        (1, 0, 0, 12), (1, 0, 1, 5), (1, 0, 2, 12),
        (1, 1, 0, 12), (1, 1, 1, 5), (1, 1, 2, 12),
        (2, 0, 0, 12), (2, 0, 1, 12),
    ]


def test_tight_radius_stops_crediting_the_path_beyond_a_junction(conn):
    # A run passes the start of a side path without turning onto it: about
    # 4.7 m from the path's end node and 14.3 m from the next node up.
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (0, 60)))])
    add_run(conn, "r", (-40, -8), (40, 18))
    recompute(conn, SETTINGS)   # 20 m spills onto the second node
    assert hits(conn, 1) == ["r", "r", None, None]

    tight = dataclasses.replace(SETTINGS, match_radius_cartpath_m=10,
                                match_radius_cartpath_end_m=10)
    recompute(conn, tight)
    assert hits(conn, 1) == ["r", None, None, None]


def test_radius_boundary(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (40, 0))),
        cartpath(2, line((0, 1000), (40, 1000))),
    ])
    inside = add_run(conn, "inside", (-50, 19.99), (90, 19.99))
    outside = add_run(conn, "outside", (-50, 1020.01), (90, 1020.01))
    match(conn, SETTINGS, activity_ids=[inside, outside])
    assert hits(conn, 1) == ["inside"] * 3
    assert hits(conn, 2) == [None] * 3


# ---- earliest run wins ----------------------------------------------------------


def test_earliest_run_wins_regardless_of_match_order(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    along = ((-20, 0), (60, 0))
    june = add_run(conn, "june", *along, start="2024-06-01T12:00:00+00:00")
    match(conn, SETTINGS, activity_ids=[june])
    assert hits(conn, 1) == ["june"] * 3

    may = add_run(conn, "may", *along, start="2024-05-01T12:00:00+00:00")   # synced late
    match(conn, SETTINGS, activity_ids=[may])
    assert hits(conn, 1) == ["may"] * 3

    july = add_run(conn, "july", *along, start="2024-07-01T12:00:00+00:00")
    assert match(conn, SETTINGS, activity_ids=[july]) == 0
    assert hits(conn, 1) == ["may"] * 3


# ---- tunnel rule ----------------------------------------------------------------

# Nodes at x = 0, 20, ... 100. This loop passes 5 m from each end but at least
# 25 m from every interior node, like a run whose GPS dropped in the tunnel.
AROUND = ((-50, 0), (-5, 0), (-5, 60), (105, 60), (105, 0), (150, 0))


def test_run_hitting_both_ends_is_credited_with_the_whole_tunnel(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")])
    add_run(conn, "t", *AROUND)
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == ["t"] * 6


def test_both_ends_rule_applies_only_to_tunnels(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)), type_="Path")])
    add_run(conn, "t", *AROUND)
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == ["t", None, None, None, None, "t"]


def test_one_end_is_not_enough(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")])
    add_run(conn, "t", (-50, 0), (-5, 0), (-5, 60))
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == ["t", None, None, None, None, None]


def test_ends_hit_by_different_runs_are_not_enough(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")])
    add_run(conn, "west", (-50, 0), (-5, 0))
    add_run(conn, "east", (105, 0), (150, 0))
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == ["west", None, None, None, None, "east"]


def test_earlier_direct_hit_inside_a_tunnel_is_kept(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")])
    add_run(conn, "early", (41, -10), (41, 10), start="2024-01-01T12:00:00+00:00")   # near x=40, 60
    add_run(conn, "t", *AROUND, start="2024-05-01T12:00:00+00:00")
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == ["t", "t", "early", "early", "t", "t"]


# ---- metrics ----------------------------------------------------------------------


def set_hits(conn, layer, oid, nodes):
    """Mark (part_idx, seq) nodes of one segment as hit, bypassing matching."""
    for part, seq in nodes:
        conn.execute("""
            UPDATE node n SET hit_at = now() FROM segment s
            WHERE s.id = n.segment_id AND s.layer = %s AND s.source_oid = %s
              AND n.part_idx = %s AND n.seq = %s
        """, (layer, oid, part, seq))


def test_cartpath_completion_needs_every_node(conn):
    rules = excl.parse({"cartpaths": [{"global_id": global_id(3), "reason": "Gated"}]})
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (40, 0))),
        cartpath(2, line((0, 50), (40, 50))),
        cartpath(3, line((0, 100), (40, 100))),   # excluded
        cartpath(4, line((0, 150), (40, 150)), type_="Road Crossing"),   # not counted
    ], exclusions=rules)
    set_hits(conn, "cartpath", 1, [(0, 0), (0, 1), (0, 2)])
    set_hits(conn, "cartpath", 2, [(0, 0), (0, 1)])
    set_hits(conn, "cartpath", 3, [(0, 0), (0, 1), (0, 2)])
    m = metrics(conn)
    assert (m.segments_complete, m.segments_total) == (1, 2)
    assert m.cartpath_complete_m == pytest.approx(40)
    assert m.cartpath_total_m == pytest.approx(80)
    assert m.cartpath_pct == pytest.approx(50)


def test_road_coverage_counts_hit_intervals_within_a_part(conn):
    # Part 0: nodes at x = 0..100 (5 intervals of 20 m). Part 1: x = 200..240.
    run_import(conn, "road", [road(1, line((0, 0), (100, 0)), line((200, 0), (240, 0)))])
    set_hits(conn, "road", 1, [(0, 0), (0, 1), (0, 2), (0, 4), (0, 5), (1, 0)])
    m = metrics(conn)
    # (0-1), (1-2), (4-5) are covered. Part 0's last node and part 1's first
    # node are both hit, but that's a gap between parts, not an interval.
    assert m.road_covered_m == pytest.approx(60)
    assert m.road_total_m == pytest.approx(140)


# ---- recompute ------------------------------------------------------------------


def test_recompute_clears_hits_without_a_run(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    set_hits(conn, "cartpath", 1, [(0, 0)])
    recompute(conn, SETTINGS)
    assert hits(conn, 1) == [None] * 3


def test_recompute_resplits_tracks_with_the_current_gap_setting(conn):
    run_import(conn, "cartpath", [cartpath(1, line((400, 0), (600, 0)))])
    # An 800 m GPS jump straight along the cart path.
    add_run(conn, "jump", (0, 0), (50, 0), (100, 0), (900, 0), (950, 0), (1000, 0), raw=True)
    recompute(conn, SETTINGS)
    assert set(hits(conn, 1)) == {None}

    recompute(conn, dataclasses.replace(SETTINGS, track_gap_split_m=1000))
    assert set(hits(conn, 1)) == {"jump"}


# ---- hooks ------------------------------------------------------------------------


def test_sync_matches_its_new_runs(conn):
    city(conn)   # cart path 1 runs along y = 0, nodes every 20 m
    client = FakeIntervals([run("i1")], {"i1": walk(conn, (0, 0), (190, 0))})
    report = sync(conn, client, date(2024, 5, 1), date(2024, 5, 31), SETTINGS)
    hit = [h for h in hits(conn, 1) if h]
    assert hit == ["i1"] * 11   # x = 0 ... 200
    assert report.newly_hit == 11


def test_dry_run_sync_leaves_no_hits(conn):
    city(conn)
    client = FakeIntervals([run("i1")], {"i1": walk(conn, (0, 0), (190, 0))})
    report = sync(conn, client, date(2024, 5, 1), date(2024, 5, 31), SETTINGS, dry_run=True)
    assert report.newly_hit == 11
    assert conn.execute("SELECT count(*) FROM node WHERE hit_at IS NOT NULL").fetchone()[0] == 0


def test_reimported_segment_is_matched_against_stored_runs(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 500), (100, 500)))])
    add_run(conn, "r", (0, 0), (100, 0))
    recompute(conn, SETTINGS)
    assert set(hits(conn, 1)) == {None}

    run_import(conn, "cartpath", [cartpath(1, line((0, 5), (100, 5)))])   # city moved it
    assert set(hits(conn, 1)) == {"r"}


# ---- near misses ------------------------------------------------------------------


def test_two_networks_do_not_cross_credit_hits_with_identical_geometry(conn):
    """The node<->activity_piece join is purely spatial; only network_id
    correlation keeps two networks' runs from crediting each other's nodes.
    Uses identical geometry for both networks -- the actual stress case,
    not just physically separate coordinates."""
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    add_network(conn, "testworld")
    run_import(conn, "cartpath", [cartpath(101, line((0, 0), (100, 0)))], network="testworld")

    add_run(conn, "ptc-run", (0, 0), (100, 0), network="ptc")
    add_run(conn, "tw-run", (0, 0), (100, 0), network="testworld")

    recompute(conn, SETTINGS, network="ptc")
    recompute(conn, SETTINGS, network="testworld")

    assert set(hits(conn, 1)) == {"ptc-run"}
    assert set(hits(conn, 101)) == {"tw-run"}

    ptc_metrics, tw_metrics = metrics(conn, "ptc"), metrics(conn, "testworld")
    assert (ptc_metrics.segments_complete, ptc_metrics.segments_total) == (1, 1)
    assert (tw_metrics.segments_complete, tw_metrics.segments_total) == (1, 1)


def test_near_misses_bucket_missed_nodes_by_distance(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 23), (10, 23))),
        cartpath(2, line((0, 50), (10, 50))),
        cartpath(3, line((0, 500), (10, 500))),
    ])
    add_run(conn, "r", (-50, 0), (60, 0))
    recompute(conn, SETTINGS)
    assert near_misses(conn)["cartpath"] == {
        "<10 m": 0, "<15 m": 0, "<20 m": 0, "<25 m": 2, "<30 m": 0, "<40 m": 0, "<60 m": 2,
        "farther": 2}
    assert set(near_misses(conn)["road"].values()) == {0}
