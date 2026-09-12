import math

import pytest

from app.graph import build_graph
from app.routing import GraphMissing, RouteError, leg, snap
from tests.helpers import SETTINGS, X0, Y0, cartpath, line, road, run_import

MAX_M = SETTINGS.route_snap_max_m


def latlon(conn, x, y):
    """UTM offsets from the fixture origin -> (lat, lon), as the map would send."""
    lon, lat = conn.execute(
        "SELECT ST_X(p), ST_Y(p) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 32616), 4326) p",
        (X0 + x, Y0 + y),
    ).fetchone()
    return lat, lon


def utm(conn, lat, lon):
    x, y = conn.execute(
        "SELECT ST_X(p), ST_Y(p) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 32616) p",
        (lon, lat),
    ).fetchone()
    return round(x - X0, 2), round(y - Y0, 2)


def route(conn, a, b):
    return leg(conn, latlon(conn, *a), latlon(conn, *b), MAX_M)


def build(conn, cartpaths=(), roads=()):
    if cartpaths:
        run_import(conn, "cartpath", list(cartpaths))
    if roads:
        run_import(conn, "road", list(roads))
    build_graph(conn, SETTINGS)


def assert_continuous(conn, r, max_gap_m=25):
    pts = [utm(conn, *p) for p in r.latlngs]
    gaps = [math.dist(p, q) for p, q in zip(pts, pts[1:])]
    assert max(gaps, default=0) <= max_gap_m
    assert sum(gaps) == pytest.approx(r.length_m, abs=0.05)
    return pts


def test_leg_along_one_line_starts_and_ends_on_the_clicks(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = route(conn, (10, 5), (80, -5))   # clicks 5 m off the path
    assert r.length_m == pytest.approx(70, abs=0.05)
    pts = assert_continuous(conn, r, max_gap_m=70)
    assert pts[0] == pytest.approx((10, 0), abs=0.05)
    assert pts[-1] == pytest.approx((80, 0), abs=0.05)


def test_leg_on_the_same_edge_backwards(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = route(conn, (80, 0), (10, 0))
    assert r.length_m == pytest.approx(70, abs=0.05)
    pts = assert_continuous(conn, r, max_gap_m=70)
    assert pts[0] == pytest.approx((80, 0), abs=0.05)
    assert pts[-1] == pytest.approx((10, 0), abs=0.05)


def test_leg_turns_at_a_t_junction(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 0), (50, 60)))])
    r = route(conn, (10, 0), (50, 50))
    assert r.length_m == pytest.approx(40 + 50, abs=0.05)
    pts = assert_continuous(conn, r, max_gap_m=50)
    assert (50, 0) in [pytest.approx(p, abs=0.05) for p in pts]


def test_leg_never_uses_a_crossing(conn):
    # A road crosses over a tunnel at (50, 0). The only real connection is a
    # path from the tunnel's east end to the road's north end.
    build(conn,
          cartpaths=[cartpath(1, line((0, 0), (100, 0)), type_="Tunnel"),
                     cartpath(2, line((100, 0), (50, 50)))],
          roads=[road(1, line((50, -50), (50, 50)))])
    r = route(conn, (20, 0), (50, 40))
    # 80 m along the tunnel, the 70.7 m connector, then 10 m down the road;
    # not the 70 m it would be through the crossing.
    assert r.length_m == pytest.approx(80 + math.hypot(50, 50) + 10, abs=0.1)
    assert_continuous(conn, r, max_gap_m=81)   # the tunnel is one straight 80 m stretch


def test_click_too_far_from_the_network(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    with pytest.raises(RouteError, match="more than 50 m"):
        snap(conn, *latlon(conn, 50, 60), MAX_M)


def test_unreachable_island(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 300), (100, 300)))])
    with pytest.raises(RouteError, match="island"):
        route(conn, (10, 0), (10, 300))


def test_missing_graph(conn):
    with pytest.raises(GraphMissing):
        snap(conn, *latlon(conn, 0, 0), MAX_M)


def test_same_point_is_a_zero_length_leg(conn):
    build(conn, [cartpath(1, line((0, 0), (100, 0)))])
    r = route(conn, (30, 0), (30, 0))
    assert r.length_m == 0 and len(r.latlngs) == 1
