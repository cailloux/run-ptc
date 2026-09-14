import math

import pytest

from app.exclusions import CartpathExclusion, Exclusions
from app.finish import finish_route, new_miles, units
from app.graph import build_graph
from app.routing import RouteError
from tests.helpers import SETTINGS, cartpath, global_id, line, run_import
from tests.test_api_route import point
from tests.test_api_sync import api  # noqa: F401  (fixture)
from tests.test_routing import build, latlon, route, utm

MAX_M = SETTINGS.route_snap_max_m


def hit(conn, oid, seqs):
    conn.execute("""
        UPDATE node SET hit_at = now() WHERE seq = ANY(%s)
          AND segment_id = (SELECT id FROM segment WHERE layer = 'cartpath' AND source_oid = %s)
    """, (list(seqs), oid))


def seg_id(conn, oid, layer="cartpath"):
    return conn.execute("SELECT id FROM segment WHERE layer = %s AND source_oid = %s",
                        (layer, oid)).fetchone()[0]


def along(conn, *pts):
    """A route through these points, routed leg by leg."""
    out = []
    for a, b in zip(pts, pts[1:]):
        out += route(conn, a, b).latlngs[1 if out else 0:]
    return out


def path_length(conn, latlngs):
    pts = [utm(conn, *p) for p in latlngs]
    return sum(math.dist(p, q) for p, q in zip(pts, pts[1:]))


# ---- new miles ---------------------------------------------------------------------


def test_all_unrun_path_is_new(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = new_miles(conn, along(conn, (0, 0), (100, 0)))
    assert r.new_m == pytest.approx(100, abs=5)
    assert len(r.lines) == 1


def test_only_the_unrun_part_is_new(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    hit(conn, 1, [0, 1, 2])   # nodes every 20 m: 0-40 m is run
    r = new_miles(conn, along(conn, (0, 0), (100, 0)))
    assert r.new_m == pytest.approx(60, abs=5)
    assert utm(conn, *r.lines[0][0])[0] == pytest.approx(40, abs=5)


def test_out_and_back_counts_once(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = new_miles(conn, along(conn, (0, 0), (100, 0), (0, 0)))
    assert r.new_m == pytest.approx(100, abs=5)


def test_out_and_back_turning_mid_interval_counts_once(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = new_miles(conn, along(conn, (0, 0), (50, 0), (0, 0)))
    assert r.new_m == pytest.approx(50, abs=5)


def test_excluded_and_uncounted_ground_is_never_new(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (200, 0)), type_="Parking"),   # not a counted type
        cartpath(3, line((200, 0), (300, 0))),
    ], Exclusions(cartpaths=(CartpathExclusion(global_id(3), "gated"),)))
    build_graph(conn, SETTINGS)
    r = new_miles(conn, along(conn, (0, 0), (300, 0)))
    assert r.new_m == pytest.approx(100, abs=5)


def test_no_route_no_new_miles(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    assert new_miles(conn, []).new_m == 0
    assert new_miles(conn, [latlon(conn, 5, 0)]).new_m == 0


# ---- finish segments ------------------------------------------------------------


def finish(conn, start, oids):
    return finish_route(conn, latlon(conn, *start), [seg_id(conn, o) for o in oids], MAX_M)


def leg_points(conn, f):
    return [f.start] + [p for lg in f.legs for p in lg["latlngs"][1:]]


def test_finishes_a_path_and_comes_back(conn):
    # A T: path 1 along the x axis, path 2 up from its middle. Start on path 1.
    build(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 0), (50, 80)))])
    hit(conn, 1, range(6))
    f = finish(conn, (10, 0), [2])
    pts = [utm(conn, *p) for p in leg_points(conn, f)]
    assert pts[0] == pytest.approx((10, 0), abs=0.05)
    assert pts[-1] == pytest.approx((10, 0), abs=0.05)
    assert max(p[1] for p in pts) == pytest.approx(80, abs=0.05)   # reaches the end of path 2
    # 40 m to the junction, 80 m up and back, 40 m home.
    assert f.length_m == pytest.approx(240, abs=0.5)
    assert path_length(conn, leg_points(conn, f)) == pytest.approx(f.length_m, abs=0.5)
    assert f.skipped == 0
    # And the whole of path 2 is new ground on the built route.
    assert new_miles(conn, leg_points(conn, f)).new_m == pytest.approx(80, abs=5)


def test_only_edges_with_unrun_stretches_are_required(conn):
    # Path 2 runs up from the corner of path 1; path 3 splits it at 50 m.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((0, 0), (0, 100))),
        cartpath(3, line((0, 50), (-80, 50))),
    ])
    hit(conn, 2, [2, 3, 4, 5])   # nodes every 20 m: 40-100 m is run
    [u] = units(conn, [seg_id(conn, 2)])
    assert len(u.edges) == 1     # the 0-50 m edge only
    f = finish(conn, (50, 0), [2])
    # 50 m to the corner, 50 m up and back, 50 m home; not the whole path.
    assert f.length_m == pytest.approx(200, abs=0.5)


def test_visits_every_picked_segment(conn):
    # A plus sign: four arms off a junction at the origin. Start at the end of one.
    build(conn, [
        cartpath(1, line((-100, 0), (0, 0))),
        cartpath(2, line((0, 0), (100, 0))),
        cartpath(3, line((0, 0), (0, 100))),
        cartpath(4, line((0, 0), (0, -100))),
    ])
    hit(conn, 1, range(6))
    f = finish(conn, (-100, 0), [2, 3, 4])
    pts = [utm(conn, *p) for p in leg_points(conn, f)]
    for end in [(100, 0), (0, 100), (0, -100)]:
        assert min(math.dist(p, end) for p in pts) < 0.1
    # Out along arm 1, each arm out and back, home: the optimum.
    assert f.length_m == pytest.approx(100 + 3 * 200 + 100, abs=0.5)


def test_a_through_street_splits_at_a_side_branch(conn):
    # A branch off the middle of a required street should be a stop the
    # router can use, not force the whole street to be walked as one block.
    build(conn, [
        cartpath(1, line((0, 0), (200, 0))),
        cartpath(2, line((100, 0), (100, 80))),
    ])
    found = units(conn, [seg_id(conn, 1), seg_id(conn, 2)])
    assert len(found) == 3
    lengths = []
    for u in found:
        edge_ids = [e for e, _, _ in u.edges]
        lengths.append(round(conn.execute(
            "SELECT sum(length_m) FROM route_edge WHERE id = ANY(%s)", (edge_ids,)).fetchone()[0]))
    assert sorted(lengths) == [80, 100, 100]   # not [80, 200]: the street isn't one block


def test_a_side_spur_does_not_require_re_walking_the_through_street(conn):
    # A through street A-B-C (100 m each half), fully unrun, with a dead-end
    # spur off its middle (B), plus a shortcut road (already run) from home
    # straight to C that's shorter than looping back through B and A. If the
    # through street can only be entered/exited at A or C, reaching the spur
    # at B forces a detour back through the just-run B-C stretch and, since
    # that's cheaper than the shortcut, the algorithm takes it: A-B-spur-B-C
    # walked, then B-C retraced to reach B, then out and back on the spur,
    # then A-B walked again to get home. Splitting the through street at B
    # removes that: A-B, spur, B-C, then the shortcut home, once each.
    build(conn, [
        cartpath(1, line((0, 0), (200, 0))),          # through street A-B-C
        cartpath(2, line((100, 0), (100, 80))),        # dead-end spur off B
        cartpath(3, line((0, 0), (0, -30))),           # home-A, already run
        cartpath(4, line((0, -30), (200, 0))),         # home-C shortcut, already run
    ])
    for oid in (3, 4):
        hit(conn, oid, range(20))
    f = finish(conn, (0, -30), [1, 2])
    # home->A (30) + A-B (100) + spur out and back (160) + B-C (100)
    # + C->home (the direct shortcut, sqrt(200**2 + 30**2) =~ 202.24).
    assert f.length_m == pytest.approx(592.24, abs=1)


def test_order_beats_greedy(conn):
    # One line along the x axis in pieces; three short pieces are unrun:
    # -25..-15, 10..20, and 100..110. From 0, greedy takes 10..20, then
    # -25..-15, then 100..110 (310 m). The best round trip is 2 x 25 + 2 x 110.
    xs = [-200, -25, -15, 0, 10, 20, 100, 110, 200]
    build(conn, [cartpath(i, line((a, 0), (b, 0))) for i, (a, b) in enumerate(zip(xs, xs[1:]), 1)])
    todo = {2: (-25, -15), 5: (10, 20), 7: (100, 110)}
    for oid in set(range(1, len(xs))) - set(todo):
        hit(conn, oid, range(20))
    f = finish(conn, (0, 0), list(todo))
    assert f.length_m == pytest.approx(270, abs=0.5)


def test_whole_segment_run_has_nothing_left(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    hit(conn, 1, range(6))
    with pytest.raises(RouteError, match="nothing left"):
        finish(conn, (10, 0), [1])


def test_islands_are_skipped(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 300), (100, 300)))])
    f = finish(conn, (0, 0), [1, 2])
    assert f.skipped == 1
    assert f.length_m == pytest.approx(200, abs=0.5)   # path 1 and back
    with pytest.raises(RouteError, match="island"):
        finish(conn, (0, 0), [2])


# ---- API ----------------------------------------------------------------------------


def test_finish_api(api, conn):
    client, _ = api
    build(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 0), (50, 80)))])
    resp = client.post("/route/finish", json={"start": point(conn, 10, 0), "segment_ids": [seg_id(conn, 2)]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["length_m"] == pytest.approx(240, abs=0.5)
    assert body["legs"][-1]["to"] == pytest.approx(body["from"], abs=1e-6)
    cov = client.post("/route/coverage", json={"latlngs": [p for lg in body["legs"] for p in lg["latlngs"]]})
    assert cov.status_code == 200
    # 40 m of path 1 and 80 m of path 2 are new; the way back isn't.
    assert cov.json()["new_m"] == pytest.approx(120, abs=5)

    hit(conn, 2, range(5))
    resp = client.post("/route/finish", json={"start": point(conn, 10, 0), "segment_ids": [seg_id(conn, 2)]})
    assert resp.status_code == 422


def test_finish_api_without_a_graph(api, conn):
    client, _ = api
    resp = client.post("/route/finish", json={"start": point(conn, 10, 0), "segment_ids": [1]})
    assert resp.status_code == 503
