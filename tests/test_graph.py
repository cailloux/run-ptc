import pytest

from app.graph import build_graph
from tests.helpers import SETTINGS, cartpath, line, road, run_import


def graph(conn, cartpaths=(), roads=()):
    if cartpaths:
        run_import(conn, "cartpath", list(cartpaths))
    if roads:
        run_import(conn, "road", list(roads))
    return build_graph(conn, SETTINGS)


def edges(conn):
    """(layer:oid, part_idx, part_from, part_to, length) per edge, in a stable order."""
    return conn.execute("""
        SELECT s.layer || ':' || s.source_oid, e.part_idx, round(e.part_from::numeric, 3)::float8,
               round(e.part_to::numeric, 3)::float8, round(e.cost::numeric, 1)::float8
        FROM route_edge e JOIN segment s ON s.id = e.segment_id
        ORDER BY 1, 2, 3
    """).fetchall()


def test_ends_within_the_tolerance_join(conn):
    report = graph(conn, [cartpath(1, line((0, 0), (50, 0))), cartpath(2, line((52.5, 0), (100, 0)))])
    assert report.components == 1


def test_ends_beyond_the_tolerance_do_not(conn):
    report = graph(conn, [cartpath(1, line((0, 0), (50, 0))), cartpath(2, line((53.5, 0), (100, 0)))])
    assert report.components == 2


def test_t_junction_splits_the_through_path(conn):
    report = graph(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 0), (50, 60)))])
    assert report.components == 1
    assert edges(conn) == [
        ("cartpath:1", 0, 0.0, 0.5, 50.0),
        ("cartpath:1", 0, 0.5, 1.0, 50.0),
        ("cartpath:2", 0, 0.0, 1.0, 60.0),
    ]


def test_t_junction_within_tolerance_but_not_touching(conn):
    # The side path stops 2 m short of the through path.
    report = graph(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 2), (50, 60)))])
    assert report.components == 1
    assert len(edges(conn)) == 3


def test_crossing_lines_never_join(conn):
    report = graph(conn, cartpaths=[cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")],
                   roads=[road(1, line((50, -50), (50, 50)))])
    assert report.components == 2
    assert len(edges(conn)) == 2


def test_a_road_ending_on_a_tunnel_does_not_connect(conn):
    report = graph(conn, cartpaths=[cartpath(1, line((0, 0), (100, 0)), type_="Tunnel")],
                   roads=[road(1, line((50, 0), (50, 60)))])
    assert report.components == 2


def test_a_road_ending_on_a_path_does_connect(conn):
    report = graph(conn, cartpaths=[cartpath(1, line((0, 0), (100, 0)))],
                   roads=[road(1, line((50, 0), (50, 60)))])
    assert report.components == 1


def test_multipart_parts_are_separate_edges_and_touching_parts_rejoin(conn):
    report = graph(conn, [cartpath(1, line((0, 0), (40, 0)), line((40, 0), (80, 0)), line((200, 0), (240, 0)))])
    assert [(e[1], e[4]) for e in edges(conn)] == [(0, 40.0), (1, 40.0), (2, 40.0)]
    assert report.components == 2   # the third part is 120 m away


def test_short_self_loops_are_dropped(conn):
    # A 2.5 m sliver whose two ends collapse onto one junction.
    report = graph(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((100, 0.5), (102, 1)))])
    assert [e[0] for e in edges(conn)] == ["cartpath:1"]
    assert report.components == 1


def test_islands_are_reported_with_their_segments(conn):
    report = graph(conn, cartpaths=[cartpath(1, line((0, 0), (500, 0))), cartpath(7, line((0, 300), (80, 300)))],
                   roads=[road(9, line((80, 300), (120, 300)))])
    assert report.components == 2
    [island] = report.islands
    assert island.segments == ["cartpath:7", "road:9"]
    assert island.length_m == pytest.approx(120)
    assert report.main_m == pytest.approx(500)


def test_rebuild_replaces_the_graph(conn):
    graph(conn, [cartpath(1, line((0, 0), (100, 0)))])
    build_graph(conn, SETTINGS)
    assert len(edges(conn)) == 1


def test_edge_fractions_map_nodes_onto_edges(conn):
    # Path 1's nodes sit at x = 0, 20, ... 100 (seq 0-5); the T at x = 50
    # splits it into edges covering [0, 0.5] and [0.5, 1].
    graph(conn, [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((50, 0), (50, 60)))])
    rows = conn.execute("""
        SELECT n.seq, e.part_from
        FROM node n
        JOIN segment s ON s.id = n.segment_id AND s.source_oid = 1
        JOIN route_edge e ON e.segment_id = n.segment_id AND e.part_idx = n.part_idx
         AND n.seq::float8 / 5 >= e.part_from AND n.seq::float8 / 5 < e.part_to
        ORDER BY n.seq
    """).fetchall()
    assert rows == [(0, 0.0), (1, 0.0), (2, 0.0), (3, 0.5), (4, 0.5)]
